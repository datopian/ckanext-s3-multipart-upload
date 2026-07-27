# encoding: utf-8
import logging
import os
import uuid

import flask
from botocore.exceptions import ClientError

import ckan.model as model
import ckan.logic as logic
from ckan.common import g, request
import ckantoolkit as toolkit

from ckanext.s3_multipart_upload.uploader import MultipartUploader

log = logging.getLogger(__name__)

NotFound = logic.NotFound
NotAuthorized = logic.NotAuthorized
ValidationError = logic.ValidationError


def _guess_format(filename):
    """Same extension-based format guess as ckanext-s3filestore's own
    S3ResourceUploader (the classic upload path this extension sits
    alongside) - not a new heuristic, the one already established in this
    codebase for this exact purpose.

    The placeholder resource created below is the *first* resource row
    that exists for this upload, so it's the first thing IResourceController
    hooks (e.g. ckanext-xloader) see - without this, they'd see an empty
    format and skip, before the classic form submit ever gets a chance to
    set a real one.
    """
    resource_format = os.path.splitext(filename)[1][1:]
    return resource_format or None

# S3 hard limit: a multipart upload may have at most 10,000 parts.
MAX_PART_NUMBER = 10000

# Presigned per-part URLs are used within seconds of being issued (Uppy
# requests one right before it PUTs the part), so a short expiry is fine and
# reduces the window in which a leaked URL is usable.
SIGNED_PART_URL_EXPIRY = 900

s3_multipart_upload = flask.Blueprint(
    u's3_multipart_upload',
    __name__,
    url_prefix=u'/dataset/<id>/resource',
    url_defaults={u'package_type': u'dataset'},
)


def _get_context():
    return {
        'model': model,
        'session': model.Session,
        'user': g.user,
        'auth_user_obj': g.userobj,
    }


def _is_valid_uuid(value):
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _authorize(id, resource_id, mode):
    """Authorize the request against the dataset (and, for updates, the
    resource). `id` is the package id/name from the URL. Returns the
    package id.

    Aborts the request (403/404) if the caller isn't allowed to add/edit a
    resource on this dataset, or if a `mode=update` resource_id doesn't
    actually belong to this dataset.
    """
    if not _is_valid_uuid(resource_id):
        toolkit.abort(400, toolkit._(u'resource_id must be a UUID'))

    context = _get_context()

    try:
        package = toolkit.get_action(u'package_show')(context, {u'id': id})
    except NotFound:
        toolkit.abort(404, toolkit._(u'Dataset not found'))
    except NotAuthorized:
        toolkit.abort(403, toolkit._(u'Not authorized to see this dataset'))

    if mode == u'update':
        try:
            resource = toolkit.get_action(u'resource_show')(
                context, {u'id': resource_id})
        except NotFound:
            toolkit.abort(404, toolkit._(u'Resource not found'))
        if resource.get(u'package_id') != package[u'id']:
            toolkit.abort(404, toolkit._(u'Resource not found'))
        try:
            toolkit.check_access(
                u'resource_update', context, {u'id': resource_id})
        except NotAuthorized:
            toolkit.abort(
                403, toolkit._(u'Not authorized to edit this resource'))
    else:
        try:
            toolkit.check_access(
                u'resource_create', context, {u'package_id': package[u'id']})
        except NotAuthorized:
            toolkit.abort(
                403,
                toolkit._(u'Not authorized to add a resource to this dataset'))

    return package[u'id']


def _get_json_body():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        toolkit.abort(400, toolkit._(u'Expected a JSON request body'))
    return data


def _expected_key_or_abort(uploader, resource_id, filename, key):
    """Recompute the key server-side from (resource_id, filename) and
    require it match exactly what the client sent. Defense in depth: a
    resource_id passing `_authorize` only proves the caller may act on that
    resource, not that an arbitrary client-supplied `key` is the one that
    belongs to it.
    """
    expected_key, munged_filename = uploader.resource_key(resource_id, filename)
    if key != expected_key:
        toolkit.abort(400, toolkit._(u'key does not match resource_id/filename'))
    return munged_filename


def start_multipart_upload(package_type, id):
    data = _get_json_body()
    resource_id = data.get(u'resource_id')
    filename = data.get(u'filename')
    content_type = data.get(u'content_type') or u'application/octet-stream'
    mode = data.get(u'mode', u'create')

    if not resource_id or not filename:
        toolkit.abort(400, toolkit._(u'resource_id and filename are required'))

    _authorize(id, resource_id, mode)

    uploader = MultipartUploader()
    key, munged_filename = uploader.resource_key(resource_id, filename)

    client = uploader.get_s3_client()
    create_kwargs = dict(
        Bucket=uploader.bucket_name,
        Key=key,
        ContentType=content_type,
    )
    # Deliberately not sending ACL=<uploader.acl> (defaults to
    # 'public-read' in ckanext-s3filestore) here. Downloads always go
    # through get_signed_url_to_key's authenticated presigned URLs, never
    # a bare public object URL, so an object ACL was never functionally
    # necessary. On a bucket with public access disabled at the
    # bucket/account level (e.g. Cloudflare R2's "Public Access: Disabled"),
    # requesting `public-read` on CompleteMultipartUpload doesn't raise an
    # error - it silently completes but the object doesn't durably persist,
    # which is a much nastier failure mode than an upfront rejection would
    # be. Simplest fix: don't ask for a permission the object doesn't need
    # and the bucket isn't configured to grant.

    # Tagging on CreateMultipartUpload only powers the *optional*
    # pending-upload lifecycle cleanup described in the README - not the
    # upload itself - and not every S3-compatible backend implements it
    # (Cloudflare R2 returns NotImplemented for the x-amz-tagging header
    # here, while AWS S3 and MinIO accept it). Once a backend is known not
    # to support it, don't send it again - see MultipartUploader.
    # tagging_supported's docstring for why this is process-lifetime, not
    # per-request.
    if MultipartUploader.tagging_supported:
        create_kwargs[u'Tagging'] = u'ckan-status=pending'

    try:
        response = client.create_multipart_upload(**create_kwargs)
    except ClientError as e:
        if (MultipartUploader.tagging_supported and
                e.response.get(u'Error', {}).get(u'Code') == u'NotImplemented'):
            log.info(
                u'Backend does not support tagging on '
                u'CreateMultipartUpload; retrying %s without it, and '
                u'disabling tagging for the rest of this process. '
                u'Pending-upload S3 lifecycle cleanup will not apply to '
                u'this object.', key)
            MultipartUploader.tagging_supported = False
            create_kwargs.pop(u'Tagging', None)
            try:
                response = client.create_multipart_upload(**create_kwargs)
            except ClientError as e2:
                log.warning(
                    u'Failed to create multipart upload for %s: %s',
                    key, e2)
                toolkit.abort(502, toolkit._(u'Failed to start upload'))
        else:
            log.warning(u'Failed to create multipart upload for %s: %s', key, e)
            toolkit.abort(502, toolkit._(u'Failed to start upload'))

    return flask.jsonify({
        u'uploadId': response[u'UploadId'],
        u'key': key,
        u'filename': munged_filename,
    })


def sign_part(package_type, id):
    data = _get_json_body()
    resource_id = data.get(u'resource_id')
    filename = data.get(u'filename')
    key = data.get(u'key')
    upload_id = data.get(u'upload_id')
    part_number = data.get(u'part_number')
    mode = data.get(u'mode', u'create')

    if not all([resource_id, filename, key, upload_id]):
        toolkit.abort(
            400,
            toolkit._(
                u'resource_id, filename, key and upload_id are required'))
    if not isinstance(part_number, int) or not (1 <= part_number <= MAX_PART_NUMBER):
        toolkit.abort(400, toolkit._(u'Invalid part_number'))

    _authorize(id, resource_id, mode)

    uploader = MultipartUploader()
    _expected_key_or_abort(uploader, resource_id, filename, key)

    url = uploader.get_s3_client().generate_presigned_url(
        ClientMethod=u'upload_part',
        Params={
            u'Bucket': uploader.bucket_name,
            u'Key': key,
            u'UploadId': upload_id,
            u'PartNumber': part_number,
        },
        ExpiresIn=SIGNED_PART_URL_EXPIRY,
    )
    return flask.jsonify({u'url': url})


def complete_multipart_upload(package_type, id):
    data = _get_json_body()
    resource_id = data.get(u'resource_id')
    filename = data.get(u'filename')
    key = data.get(u'key')
    upload_id = data.get(u'upload_id')
    parts = data.get(u'parts') or []
    mode = data.get(u'mode', u'create')

    if not all([resource_id, filename, key, upload_id]) or not parts:
        toolkit.abort(
            400,
            toolkit._(
                u'resource_id, filename, key, upload_id and parts are '
                u'required'))

    package_id = _authorize(id, resource_id, mode)

    uploader = MultipartUploader()
    munged_filename = _expected_key_or_abort(uploader, resource_id, filename, key)

    try:
        parts_arg = [
            {u'ETag': p[u'ETag'], u'PartNumber': p[u'PartNumber']}
            for p in parts
        ]
    except (KeyError, TypeError):
        toolkit.abort(400, toolkit._(u'Each part needs ETag and PartNumber'))

    client = uploader.get_s3_client()
    try:
        client.complete_multipart_upload(
            Bucket=uploader.bucket_name,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={u'Parts': parts_arg},
        )
    except ClientError as e:
        log.warning(u'Failed to complete multipart upload for %s: %s', key, e)
        toolkit.abort(502, toolkit._(u'Failed to complete upload'))

    response = {u'filename': munged_filename}
    size = None
    mimetype = None
    try:
        # Authoritative size/content-type as actually stored on S3, rather
        # than trusting the browser's claims about the file it sent.
        head = client.head_object(Bucket=uploader.bucket_name, Key=key)
        size = head.get(u'ContentLength')
        mimetype = head.get(u'ContentType')
        response[u'size'] = size
        response[u'mimetype'] = mimetype
    except ClientError as e:
        log.warning(u'Failed to head_object after completing %s: %s', key, e)

    if mode == u'create':
        # CKAN's own resource "new" view (ckan/views/resource.py
        # CreateView.post) decides resource_create vs resource_update
        # purely by whether the form's `id` field is populated - and we
        # have to populate it up front so the browser knows the S3 key
        # before any resource row exists (see README's "client-supplied
        # UUID" section). That means that by the time the classic form is
        # eventually submitted, CKAN's view sees a non-empty `id` and
        # calls resource_update instead of resource_create - which 404s,
        # because no such resource exists yet, and the resource is never
        # actually saved.
        #
        # Work around this by creating a minimal placeholder resource row
        # here, right after the file lands on S3. By the time the user
        # submits the form, the row already exists, so CKAN's view calls
        # resource_update instead - which is exactly right, and folds in
        # whatever name/format/etc. the user filled in by then.
        placeholder_data = {
            u'package_id': package_id,
            u'id': resource_id,
            u'url': munged_filename,
            u'url_type': u'upload',
            u'size': size,
            u'mimetype': mimetype,
        }
        resource_format = _guess_format(munged_filename)
        if resource_format:
            placeholder_data[u'format'] = resource_format
        try:
            toolkit.get_action(u'resource_create')(_get_context(), placeholder_data)
        except ValidationError as e:
            log.warning(
                u'Failed to create placeholder resource %s: %s',
                resource_id, e)
            toolkit.abort(400, toolkit._(u'Failed to save resource'))

    return flask.jsonify(response)


def abort_multipart_upload(package_type, id):
    data = _get_json_body()
    resource_id = data.get(u'resource_id')
    filename = data.get(u'filename')
    key = data.get(u'key')
    upload_id = data.get(u'upload_id')
    mode = data.get(u'mode', u'create')

    if not all([resource_id, filename, key, upload_id]):
        toolkit.abort(
            400,
            toolkit._(
                u'resource_id, filename, key and upload_id are required'))

    _authorize(id, resource_id, mode)

    uploader = MultipartUploader()
    _expected_key_or_abort(uploader, resource_id, filename, key)

    try:
        uploader.get_s3_client().abort_multipart_upload(
            Bucket=uploader.bucket_name, Key=key, UploadId=upload_id)
    except ClientError as e:
        # Best-effort: log and report success anyway, there's nothing more
        # the client can do about a stray multipart upload, the bucket's
        # AbortIncompleteMultipartUpload lifecycle rule will clean it up.
        log.warning(u'Failed to abort multipart upload for %s: %s', key, e)

    return flask.jsonify({u'ok': True})


s3_multipart_upload.add_url_rule(
    u'/multipart/start', view_func=start_multipart_upload, methods=[u'POST'])
s3_multipart_upload.add_url_rule(
    u'/multipart/sign_part', view_func=sign_part, methods=[u'POST'])
s3_multipart_upload.add_url_rule(
    u'/multipart/complete', view_func=complete_multipart_upload,
    methods=[u'POST'])
s3_multipart_upload.add_url_rule(
    u'/multipart/abort', view_func=abort_multipart_upload, methods=[u'POST'])


def get_blueprints():
    return [s3_multipart_upload]

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
    """Extension-based format guess, same as S3ResourceUploader's."""
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
    """Authorize against the dataset/resource. Returns the package id."""
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
    """Recompute the key server-side and require it matches the client's."""
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
    # No ACL (downloads use signed URLs). No Tagging - DeleteObjectTagging
    # deletes objects outright on R2, see README. Orphans are cleaned up by
    # the bucket's AbortIncompleteMultipartUpload lifecycle rule instead.
    try:
        response = client.create_multipart_upload(**create_kwargs)
    except ClientError as e:
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
        # Create a placeholder resource row now, so the classic form
        # submit later (which sees a non-empty `id`) hits resource_update
        # instead of a 404ing resource_create.
        placeholder_data = {
            u'package_id': package_id,
            u'id': resource_id,
            u'name': munged_filename,
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

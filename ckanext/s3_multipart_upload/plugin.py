# encoding: utf-8
import logging

import ckan.plugins as plugins
import ckantoolkit as toolkit

from ckanext.s3_multipart_upload import blueprint
from ckanext.s3_multipart_upload import cli
from ckanext.s3_multipart_upload import helpers as h
from ckanext.s3_multipart_upload.uploader import (
    MultipartUploader,
    filename_from_url,
)

log = logging.getLogger(__name__)

FILE_METADATA_FIELDS = ('size', 'mimetype')


def _fill_upload_file_metadata(current, resource):
    """Keep size/mimetype on uploaded resources across resource_update.

    Multipart uploads never pass through CKAN's uploader, which is what
    normally sets these. The /complete endpoint does write them onto the
    create-mode placeholder, but the resource form has no size/mimetype
    inputs, so the form save that follows (a resource_update, which replaces
    the whole resource dict) drops them again. Update mode never writes them
    at all.
    """
    if resource.get('url_type') != 'upload' or resource.get('upload'):
        # Not an upload, or a classic upload CKAN's uploader will measure.
        return
    filename = filename_from_url(resource.get('url'))
    if not filename:
        return

    same_file = (
        current.get('url_type') == 'upload'
        and filename_from_url(current.get('url')) == filename)
    if same_file:
        for field in FILE_METADATA_FIELDS:
            if not resource.get(field) and current.get(field):
                resource[field] = current[field]
        if resource.get('size'):
            return

    # New file (any size passed in may be the old file's, e.g. from
    # resource_patch), or the same file that never got a size recorded.
    head = MultipartUploader().head_resource_object(resource['id'], filename)
    if not head:
        return
    if head.get('ContentLength') is not None:
        resource['size'] = head['ContentLength']
    if head.get('ContentType') and (
            not same_file or not resource.get('mimetype')):
        resource['mimetype'] = head['ContentType']


class S3MultipartUploadPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IBlueprint)
    plugins.implements(plugins.IResourceController)
    plugins.implements(plugins.ITemplateHelpers)
    plugins.implements(plugins.IClick)

    # IConfigurer

    def update_config(self, config_):
        toolkit.add_template_directory(config_, 'templates')
        toolkit.add_resource('assets', 's3_multipart_upload')

    # IBlueprint

    def get_blueprint(self):
        return blueprint.get_blueprints()

    # ITemplateHelpers

    def get_helpers(self):
        return {'s3_multipart_upload_settings': h.s3_multipart_upload_settings}

    # IClick

    def get_commands(self):
        return cli.get_commands()

    # IResourceController (all no-ops - see blueprint.py for why there's no
    # tag-clearing hook here any more)

    def after_resource_create(self, context, resource):
        pass

    def after_resource_update(self, context, resource):
        # Resource was cleared (no upload, no link) - purge any stale
        # datastore table left over from the file that used to be here.
        # CKAN doesn't do this on its own; the datastore is independent of
        # the resource's file state.
        if resource.get('url_type') or resource.get('url'):
            return
        try:
            toolkit.get_action('datastore_delete')(
                {'ignore_auth': True},
                {'resource_id': resource['id'], 'force': True})
        except Exception as e:
            log.debug(
                'No datastore table to clear for %s: %s', resource.get('id'), e)

    def before_resource_create(self, context, resource):
        pass

    def before_resource_update(self, context, current, resource):
        # Core CKAN's clear_upload handling only blanks url_type, leaving
        # the bare old filename in url - which then gets mangled into
        # "http://<filename>" since it's now treated as a Link. Blank url
        # too, before the uploader class (s3filestore) ever sees it.
        if resource.get('clear_upload') and not resource.get('upload'):
            resource['url'] = ''
        _fill_upload_file_metadata(current, resource)

    def before_resource_delete(self, context, resource, resources):
        pass

    def after_resource_delete(self, context, resources):
        pass

    def before_resource_show(self, resource_dict):
        pass

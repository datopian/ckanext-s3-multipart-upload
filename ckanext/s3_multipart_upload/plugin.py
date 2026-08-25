# encoding: utf-8
import logging

import ckan.plugins as plugins
import ckantoolkit as toolkit

from ckanext.s3_multipart_upload import blueprint
from ckanext.s3_multipart_upload import helpers as h

log = logging.getLogger(__name__)


class S3MultipartUploadPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IBlueprint)
    plugins.implements(plugins.IResourceController)
    plugins.implements(plugins.ITemplateHelpers)

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

    def before_resource_delete(self, context, resource, resources):
        pass

    def after_resource_delete(self, context, resources):
        pass

    def before_resource_show(self, resource_dict):
        pass

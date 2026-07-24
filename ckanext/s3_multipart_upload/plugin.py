# encoding: utf-8
import logging
import os

import ckan.plugins as plugins
import ckantoolkit as toolkit
from botocore.exceptions import ClientError

from ckanext.s3_multipart_upload import blueprint
from ckanext.s3_multipart_upload import helpers as h
from ckanext.s3_multipart_upload.uploader import MultipartUploader

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

    # IResourceController

    def after_resource_create(self, context, resource):
        self._confirm_upload(resource)

    def after_resource_update(self, context, resource):
        self._confirm_upload(resource)

    def before_resource_create(self, context, resource):
        pass

    def before_resource_update(self, context, current, resource):
        pass

    def before_resource_delete(self, context, resource, resources):
        pass

    def after_resource_delete(self, context, resources):
        pass

    def before_resource_show(self, resource_dict):
        pass

    def _confirm_upload(self, resource):
        """Clear the `ckan-status=pending` tag set at multipart-upload time,
        now that a real CKAN resource row points at this key. Objects still
        tagged pending are expired by a bucket lifecycle rule (see README) —
        this only matters for uploads that finished but whose form was never
        saved.
        """
        if resource.get('url_type') != 'upload' or not resource.get('url'):
            return
        try:
            uploader = MultipartUploader()
            filename = os.path.basename(resource['url'])
            key, _munged = uploader.resource_key(resource['id'], filename)
            uploader.get_s3_client().delete_object_tagging(
                Bucket=uploader.bucket_name, Key=key)
        except ClientError as e:
            log.warning(
                'Could not clear pending-upload tag for resource %s: %s',
                resource.get('id'), e)
        except Exception as e:
            log.warning(
                'Unexpected error clearing pending-upload tag for '
                'resource %s: %s', resource.get('id'), e)

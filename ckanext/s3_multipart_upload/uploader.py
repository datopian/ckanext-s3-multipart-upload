# encoding: utf-8
import os

from ckantoolkit import config
import ckan.lib.munge as munge

from ckanext.s3filestore.uploader import BaseS3Uploader


class MultipartUploader(BaseS3Uploader):
    """Reuses ckanext-s3filestore's bucket/credentials/session handling."""

    def __init__(self):
        super(MultipartUploader, self).__init__()
        path = config.get('ckanext.s3filestore.aws_storage_path', '')
        self.storage_path = os.path.join(path, 'resources')

    def resource_key(self, resource_id, filename):
        """Same key S3ResourceUploader.get_path() would produce. Returns
        (key, munged_filename)."""
        munged_filename = munge.munge_filename(filename)
        key = os.path.join(self.storage_path, resource_id, munged_filename)
        return key, munged_filename

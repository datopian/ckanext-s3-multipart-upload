# encoding: utf-8
import logging
import os

from botocore.exceptions import ClientError
from urllib.parse import unquote, urlparse

from ckantoolkit import config
import ckan.lib.munge as munge

from ckanext.s3filestore.uploader import BaseS3Uploader

log = logging.getLogger(__name__)


def filename_from_url(url):
    """Filename part of an upload resource's url. CKAN stores the bare
    filename, but package_show/resource_show expand it into a full
    .../download/<filename> URL, so handle both."""
    if not url:
        return None
    return unquote(os.path.basename(urlparse(url).path)) or None


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

    def head_resource_object(self, resource_id, filename):
        """HeadObject response for a resource's file, or None if the object
        is missing or S3 errors."""
        key, _ = self.resource_key(resource_id, filename)
        try:
            return self.get_s3_client().head_object(
                Bucket=self.bucket_name, Key=key)
        except ClientError as e:
            log.warning(u'Failed to head_object %s: %s', key, e)
            return None

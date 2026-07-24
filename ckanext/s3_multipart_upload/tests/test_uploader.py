# encoding: utf-8
import pytest

from ckanext.s3_multipart_upload.uploader import MultipartUploader
from ckanext.s3filestore.uploader import S3ResourceUploader


@pytest.mark.usefixtures(u'clean_db')
class TestResourceKey(object):

    def test_key_matches_s3_resource_uploader_get_path(self, ckan_config):
        u'''The key our uploader computes for a given (resource_id,
        filename) must be byte-identical to what
        ckanext.s3filestore.uploader.S3ResourceUploader.get_path() would
        compute for the same inputs -- otherwise a file we PUT directly to
        S3 would sit at a different key than CKAN's own classic upload path
        expects, and downloads/edits would silently miss it.
        '''
        resource_id = u'165900ba-3c60-43c5-9e9c-9f8acd0aa93f'
        filename = u'My Data File.csv'

        key, munged_filename = MultipartUploader().resource_key(
            resource_id, filename)

        expected = S3ResourceUploader({}).get_path(resource_id, munged_filename)
        assert key == expected

    def test_munges_filename(self, ckan_config):
        key, munged_filename = MultipartUploader().resource_key(
            u'165900ba-3c60-43c5-9e9c-9f8acd0aa93f', u'My Data File.csv')
        assert u' ' not in munged_filename
        assert key.endswith(munged_filename)

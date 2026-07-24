# encoding: utf-8
import pytest
from moto import mock_aws
from ckantoolkit import config

from ckanext.s3_multipart_upload.uploader import MultipartUploader


@pytest.fixture
def s3_client(ckan_config):
    """A boto3 S3 client backed by an in-process moto mock.

    The `with mock_aws():` block deliberately wraps the `yield` (not just the
    client construction) so the mock stays active for the whole lifetime of
    any test using this fixture, not just fixture setup.
    """
    with mock_aws():
        yield MultipartUploader().get_s3_client()


@pytest.fixture
def s3_bucket_name(ckan_config):
    return config.get(u'ckanext.s3filestore.aws_bucket_name')


@pytest.fixture
def s3_bucket(s3_client, s3_bucket_name):
    s3_client.create_bucket(Bucket=s3_bucket_name)
    return s3_bucket_name

# encoding: utf-8
import uuid

import pytest

import ckan.tests.factories as factories
import ckan.tests.helpers as helpers
from ckan.cli.cli import ckan as ckan_cli

from ckanext.s3_multipart_upload.uploader import MultipartUploader


def _put_resource_object(s3_client, bucket, resource_id, filename, content,
                         content_type=u'text/csv'):
    key, munged_filename = MultipartUploader().resource_key(
        resource_id, filename)
    s3_client.put_object(
        Bucket=bucket, Key=key, Body=content, ContentType=content_type)
    return key, munged_filename


def _uploaded_resource(dataset, filename, **kwargs):
    """An upload resource as the /complete placeholder would create it."""
    return factories.Resource(
        package_id=dataset[u'id'],
        id=kwargs.pop(u'id', str(uuid.uuid4())),
        url=filename,
        url_type=u'upload',
        **kwargs)


def _form_save(resource, **changes):
    """resource_update with only what the resource form posts: no size or
    mimetype inputs exist on the form."""
    data = {
        u'id': resource[u'id'],
        u'package_id': resource[u'package_id'],
        u'name': resource[u'name'],
        u'url': resource[u'url'],
        u'url_type': resource[u'url_type'],
    }
    data.update(changes)
    return helpers.call_action(u'resource_update', **data)


@pytest.mark.usefixtures(u'clean_db', u'clean_index')
class TestUploadFileMetadataHook(object):

    def test_form_save_keeps_existing_size_without_s3(
            self, s3_client, s3_bucket):
        dataset = factories.Dataset()
        # No S3 object: the size must come from the current resource.
        resource = _uploaded_resource(
            dataset, u'data.csv', size=1234, mimetype=u'text/csv')

        updated = _form_save(resource, name=u'Renamed')

        assert updated[u'size'] == 1234
        assert updated[u'mimetype'] == u'text/csv'

    def test_form_save_fills_missing_size_from_s3(self, s3_client, s3_bucket):
        dataset = factories.Dataset()
        resource_id = str(uuid.uuid4())
        content = b'a,b,c\n1,2,3\n'
        _, filename = _put_resource_object(
            s3_client, s3_bucket, resource_id, u'data.csv', content)
        resource = _uploaded_resource(dataset, filename, id=resource_id)
        assert resource[u'size'] is None

        updated = _form_save(resource)

        assert updated[u'size'] == len(content)
        assert updated[u'mimetype'] == u'text/csv'

    def test_new_file_replaces_stale_size(self, s3_client, s3_bucket):
        dataset = factories.Dataset()
        resource_id = str(uuid.uuid4())
        resource = _uploaded_resource(
            dataset, u'old.csv', id=resource_id, size=999,
            mimetype=u'text/csv')
        content = b'{"a": 1}'
        _, filename = _put_resource_object(
            s3_client, s3_bucket, resource_id, u'new.json', content,
            content_type=u'application/json')

        # resource_patch merges the old size/mimetype back in.
        updated = helpers.call_action(
            u'resource_patch', id=resource_id, url=filename)

        assert updated[u'size'] == len(content)
        assert updated[u'mimetype'] == u'application/json'

    def test_link_resource_untouched(self, s3_client, s3_bucket):
        dataset = factories.Dataset()
        resource = factories.Resource(
            package_id=dataset[u'id'], url=u'https://example.com/data.csv')

        updated = _form_save(resource, url_type=u'')

        assert updated[u'size'] is None


@pytest.mark.usefixtures(u'clean_db', u'clean_index')
class TestBackfillSizesCommand(object):

    def test_backfills_missing_sizes(self, cli, s3_client, s3_bucket):
        dataset = factories.Dataset()
        resource_id = str(uuid.uuid4())
        content = b'x,y\n1,2\n'
        _, filename = _put_resource_object(
            s3_client, s3_bucket, resource_id, u'data.csv', content)
        resource = _uploaded_resource(dataset, filename, id=resource_id)
        orphan = _uploaded_resource(dataset, u'gone.csv')

        result = cli.invoke(
            ckan_cli, [u's3-multipart-upload', u'backfill-sizes'])

        assert result.exit_code == 0, result.output
        shown = helpers.call_action(u'resource_show', id=resource[u'id'])
        assert shown[u'size'] == len(content)
        assert shown[u'mimetype'] == u'text/csv'
        assert helpers.call_action(
            u'resource_show', id=orphan[u'id'])[u'size'] is None
        searched = helpers.call_action(
            u'package_search', q=u'id:{}'.format(dataset[u'id']))
        assert searched[u'results'][0][u'resources'][0][u'size'] == len(content)

    def test_dry_run_writes_nothing(self, cli, s3_client, s3_bucket):
        dataset = factories.Dataset()
        resource_id = str(uuid.uuid4())
        _, filename = _put_resource_object(
            s3_client, s3_bucket, resource_id, u'data.csv', b'x\n1\n')
        resource = _uploaded_resource(dataset, filename, id=resource_id)

        result = cli.invoke(
            ckan_cli,
            [u's3-multipart-upload', u'backfill-sizes', u'--dry-run'])

        assert result.exit_code == 0, result.output
        assert helpers.call_action(
            u'resource_show', id=resource[u'id'])[u'size'] is None

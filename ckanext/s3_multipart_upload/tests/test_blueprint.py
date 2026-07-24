# encoding: utf-8
import uuid

import pytest
import requests

import ckan.tests.factories as factories
from ckan.lib.helpers import url_for

from ckanext.s3_multipart_upload.uploader import MultipartUploader


# CKAN's test-core.ini disables Flask-WTF's CSRF protection, so the test app
# can POST JSON directly without carrying a token -- consistent with every
# other POST-based blueprint test in this stack's extensions.


@pytest.mark.usefixtures(u'clean_db', u'clean_index')
class TestMultipartUploadBlueprint(object):

    def _start_url(self, package_id):
        return url_for(u's3_multipart_upload.start_multipart_upload', id=package_id)

    def _sign_part_url(self, package_id):
        return url_for(u's3_multipart_upload.sign_part', id=package_id)

    def _complete_url(self, package_id):
        return url_for(
            u's3_multipart_upload.complete_multipart_upload', id=package_id)

    def _abort_url(self, package_id):
        return url_for(u's3_multipart_upload.abort_multipart_upload', id=package_id)

    def test_start_requires_authorized_user(self, app, s3_bucket):
        dataset = factories.Dataset()
        other_user = factories.User()

        response = app.post_json(
            self._start_url(dataset[u'id']),
            {
                u'resource_id': str(uuid.uuid4()),
                u'filename': u'data.csv',
                u'mode': u'create',
            },
            extra_environ={u'REMOTE_USER': str(other_user[u'name'])},
            status=403,
        )
        assert response.status_code == 403

    def test_start_returns_key_matching_uploader(self, app, s3_bucket):
        user = factories.Sysadmin()
        dataset = factories.Dataset(user=user)
        resource_id = str(uuid.uuid4())

        response = app.post_json(
            self._start_url(dataset[u'id']),
            {
                u'resource_id': resource_id,
                u'filename': u'My Data.csv',
                u'content_type': u'text/csv',
                u'mode': u'create',
            },
            extra_environ={u'REMOTE_USER': str(user[u'name'])},
        )
        data = response.json
        assert data[u'uploadId']

        expected_key, expected_filename = MultipartUploader().resource_key(
            resource_id, u'My Data.csv')
        assert data[u'key'] == expected_key
        assert data[u'filename'] == expected_filename

    def test_sign_part_rejects_mismatched_key(self, app, s3_bucket):
        user = factories.Sysadmin()
        dataset = factories.Dataset(user=user)
        resource_id = str(uuid.uuid4())
        env = {u'REMOTE_USER': str(user[u'name'])}

        started = app.post_json(
            self._start_url(dataset[u'id']),
            {
                u'resource_id': resource_id,
                u'filename': u'data.csv',
                u'mode': u'create',
            },
            extra_environ=env,
        ).json

        response = app.post_json(
            self._sign_part_url(dataset[u'id']),
            {
                u'resource_id': resource_id,
                # wrong filename -> recomputed key won't match started key
                u'filename': u'other.csv',
                u'key': started[u'key'],
                u'upload_id': started[u'uploadId'],
                u'part_number': 1,
                u'mode': u'create',
            },
            extra_environ=env,
            status=400,
        )
        assert response.status_code == 400

    def test_update_mode_rejects_resource_from_another_package(
            self, app, s3_bucket):
        user = factories.Sysadmin()
        dataset_a = factories.Dataset(user=user)
        dataset_b = factories.Dataset(user=user)
        resource_in_b = factories.Resource(package_id=dataset_b[u'id'])
        env = {u'REMOTE_USER': str(user[u'name'])}

        response = app.post_json(
            self._start_url(dataset_a[u'id']),
            {
                u'resource_id': resource_in_b[u'id'],
                u'filename': u'data.csv',
                u'mode': u'update',
            },
            extra_environ=env,
            status=404,
        )
        assert response.status_code == 404

    def test_full_multipart_upload_roundtrip(self, app, s3_client, s3_bucket):
        u'''start -> sign_part -> PUT bytes straight to S3 -> complete
        should leave an object at exactly the key CKAN's classic upload path
        would use, with the uploaded content intact.
        '''
        user = factories.Sysadmin()
        dataset = factories.Dataset(user=user)
        resource_id = str(uuid.uuid4())
        env = {u'REMOTE_USER': str(user[u'name'])}
        content = b'a,b,c\n1,2,3\n'

        started = app.post_json(
            self._start_url(dataset[u'id']),
            {
                u'resource_id': resource_id,
                u'filename': u'data.csv',
                u'content_type': u'text/csv',
                u'mode': u'create',
            },
            extra_environ=env,
        ).json

        signed = app.post_json(
            self._sign_part_url(dataset[u'id']),
            {
                u'resource_id': resource_id,
                u'filename': u'data.csv',
                u'key': started[u'key'],
                u'upload_id': started[u'uploadId'],
                u'part_number': 1,
                u'mode': u'create',
            },
            extra_environ=env,
        ).json

        part_response = requests.put(signed[u'url'], data=content)
        assert part_response.status_code == 200
        etag = part_response.headers[u'ETag']

        completed = app.post_json(
            self._complete_url(dataset[u'id']),
            {
                u'resource_id': resource_id,
                u'filename': u'data.csv',
                u'key': started[u'key'],
                u'upload_id': started[u'uploadId'],
                u'parts': [{u'ETag': etag, u'PartNumber': 1}],
                u'mode': u'create',
            },
            extra_environ=env,
        ).json
        assert completed[u'filename'] == started[u'filename']
        assert completed[u'size'] == len(content)

        obj = s3_client.get_object(
            Bucket=s3_bucket, Key=started[u'key'])
        assert obj[u'Body'].read() == content

    def test_abort_removes_multipart_upload(self, app, s3_client, s3_bucket):
        user = factories.Sysadmin()
        dataset = factories.Dataset(user=user)
        resource_id = str(uuid.uuid4())
        env = {u'REMOTE_USER': str(user[u'name'])}

        started = app.post_json(
            self._start_url(dataset[u'id']),
            {
                u'resource_id': resource_id,
                u'filename': u'data.csv',
                u'mode': u'create',
            },
            extra_environ=env,
        ).json

        app.post_json(
            self._abort_url(dataset[u'id']),
            {
                u'resource_id': resource_id,
                u'filename': u'data.csv',
                u'key': started[u'key'],
                u'upload_id': started[u'uploadId'],
                u'mode': u'create',
            },
            extra_environ=env,
        )

        uploads = s3_client.list_multipart_uploads(Bucket=s3_bucket)
        assert not uploads.get(u'Uploads')


@pytest.mark.usefixtures(u'clean_db', u'clean_index')
class TestConfirmUploadHook(object):

    def test_after_resource_create_clears_pending_tag(
            self, app, s3_client, s3_bucket):
        user = factories.Sysadmin()
        dataset = factories.Dataset(user=user)
        resource_id = str(uuid.uuid4())
        env = {u'REMOTE_USER': str(user[u'name'])}
        content = b'x,y\n1,2\n'

        started = app.post_json(
            url_for(
                u's3_multipart_upload.start_multipart_upload',
                id=dataset[u'id']),
            {
                u'resource_id': resource_id,
                u'filename': u'data.csv',
                u'mode': u'create',
            },
            extra_environ=env,
        ).json

        signed = app.post_json(
            url_for(u's3_multipart_upload.sign_part', id=dataset[u'id']),
            {
                u'resource_id': resource_id,
                u'filename': u'data.csv',
                u'key': started[u'key'],
                u'upload_id': started[u'uploadId'],
                u'part_number': 1,
                u'mode': u'create',
            },
            extra_environ=env,
        ).json
        part_response = requests.put(signed[u'url'], data=content)
        etag = part_response.headers[u'ETag']

        app.post_json(
            url_for(
                u's3_multipart_upload.complete_multipart_upload',
                id=dataset[u'id']),
            {
                u'resource_id': resource_id,
                u'filename': u'data.csv',
                u'key': started[u'key'],
                u'upload_id': started[u'uploadId'],
                u'parts': [{u'ETag': etag, u'PartNumber': 1}],
                u'mode': u'create',
            },
            extra_environ=env,
        )

        tags_before = s3_client.get_object_tagging(
            Bucket=s3_bucket, Key=started[u'key'])
        assert any(
            t[u'Key'] == u'ckan-status' and t[u'Value'] == u'pending'
            for t in tags_before[u'TagSet']
        )

        import ckan.tests.helpers as helpers
        helpers.call_action(
            u'resource_create',
            {u'user': user[u'name']},
            id=resource_id,
            package_id=dataset[u'id'],
            url=started[u'filename'],
            url_type=u'upload',
            name=u'data.csv',
            format=u'csv',
        )

        tags_after = s3_client.get_object_tagging(
            Bucket=s3_bucket, Key=started[u'key'])
        assert tags_after[u'TagSet'] == []

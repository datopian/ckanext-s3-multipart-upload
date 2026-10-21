# encoding: utf-8
import click

import ckan.model as model
import ckan.lib.search as search

from ckanext.s3_multipart_upload.uploader import (
    MultipartUploader,
    filename_from_url,
)


@click.group(u's3-multipart-upload')
def s3_multipart_upload():
    u'''ckanext-s3-multipart-upload commands.'''
    pass


@s3_multipart_upload.command(u'backfill-sizes')
@click.option(u'--dry-run', is_flag=True,
              help=u'Report what would change without writing anything.')
def backfill_sizes(dry_run):
    u'''Fill in size (and mimetype, when empty) for uploaded resources that
    have no size, using the object as stored on S3.

    Writes the columns directly and reindexes the affected datasets, rather
    than going through resource_patch, so it does not create activity
    entries or resubmit resources to DataPusher/xloader.
    '''
    uploader = MultipartUploader()
    resources = model.Session.query(model.Resource).filter(
        model.Resource.url_type == u'upload',
        model.Resource.state == u'active',
        model.Resource.size.is_(None),
    ).all()
    click.echo(u'{} uploaded resource(s) without a size'.format(len(resources)))

    package_ids = set()
    missing = 0
    for resource in resources:
        filename = filename_from_url(resource.url)
        head = filename and uploader.head_resource_object(
            resource.id, filename)
        if not head or head.get(u'ContentLength') is None:
            missing += 1
            click.secho(
                u'MISSING  {}  {}'.format(resource.id, filename), fg=u'yellow')
            continue

        resource.size = head[u'ContentLength']
        if not resource.mimetype and head.get(u'ContentType'):
            resource.mimetype = head[u'ContentType']
        package_ids.add(resource.package_id)
        click.echo(u'OK       {}  {}  {} bytes'.format(
            resource.id, filename, resource.size))

    if dry_run:
        model.Session.rollback()
        click.echo(u'Dry run: no changes written')
        return

    model.Session.commit()
    for package_id in package_ids:
        search.rebuild(package_id)

    click.echo(u'Updated {} resource(s) in {} dataset(s); {} without an '
               u'S3 object'.format(
                   len(resources) - missing, len(package_ids), missing))


def get_commands():
    return [s3_multipart_upload]

from setuptools import setup, find_namespace_packages

setup(
    name='ckanext-s3-multipart-upload',
    version='0.1.0',
    description="Direct-to-S3 multipart resource uploads for CKAN, via ckanext-s3filestore",
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    url='',
    author='Datopian',
    license='AGPL',
    packages=find_namespace_packages(include=['ckanext*']),
    namespace_packages=['ckanext'],
    install_requires=[
        'boto3>=1.4.4',
        'ckantoolkit',
        'ckanext-s3filestore',
    ],
    include_package_data=True,
    entry_points='''
        [ckan.plugins]
        s3_multipart_upload=ckanext.s3_multipart_upload.plugin:S3MultipartUploadPlugin
    ''',
)

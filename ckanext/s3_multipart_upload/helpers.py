# encoding: utf-8
import ckantoolkit as toolkit


def s3_multipart_upload_settings():
    return {
        'part_size_mb': int(
            toolkit.config.get('ckanext.s3_multipart_upload.part_size_mb', 25)),
        'max_file_size_mb': int(
            toolkit.config.get(
                'ckanext.s3_multipart_upload.max_file_size_mb', 5000)),
    }

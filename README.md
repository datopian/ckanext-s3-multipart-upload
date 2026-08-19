# ckanext-s3-multipart-upload

Direct-to-S3 multipart resource uploads for CKAN.

Replaces the classic "browser → CKAN app server → S3" resource-upload path
with "browser → S3" directly, using S3's native multipart upload API. CKAN
only ever hands out short-lived presigned URLs; the file's bytes never pass
through the CKAN app server. Written on top of the resource file layout and
credentials already configured for
[`ckanext-s3filestore`](https://github.com/keitaroinc/ckanext-s3filestore) —
this extension does **not** introduce a new storage backend or bucket
layout, it only changes how bytes get from the browser into the bucket.

## Requirements

- `ckanext-s3filestore`, installed, configured, and enabled — this extension
  reuses its `ckanext.s3filestore.*` config for bucket name, region,
  credentials, ACL, storage path, etc. It does not work without it.
- CKAN 2.10+ (developed/tested against 2.11).

## How it works

1. The resource form's file picker still triggers a normal OS file dialog
   (native `<input type="file">`, untouched — see "Compatibility" below).
2. Once a file is chosen, the browser:
   - generates a UUID for a new resource (CKAN's `resource_create` already
     accepts a client-supplied id — see the "Why a client-supplied UUID is
     safe" section below),
   - calls this extension's `/multipart/start` endpoint to get an S3
     `UploadId` and key,
   - uploads the file in parts directly to S3 using short-lived presigned
     per-part URLs from `/multipart/sign_part`,
   - calls `/multipart/complete` once all parts have uploaded.
3. The form is then submitted normally (name, description, etc. included),
   with `url`/`url_type=upload` already populated to point at the file that
   is now sitting on S3 — `ckanext-s3filestore`'s own upload code sees no
   file in the POST and does nothing, because there's nothing left for it to
   do.

### Why a client-supplied UUID is safe

CKAN core's resource schema
(`ckan/logic/schema.py:default_resource_schema`) validates `id` with
`[ignore_empty, uuid_validator, resource_id_does_not_exist, unicode_safe]` —
meaning `resource_create` already accepts and uses a caller-supplied UUID as
long as it's well-formed and not already taken. This is exactly what lets
the browser know the S3 key (which is keyed by resource id) before the
resource row exists.

### Compatibility with the native upload widget

This extension deliberately does **not** replace CKAN's tri-state
upload/link/clear widget (`package/snippets/resource_upload_field.html` +
`resource-upload-field.js`) or its own JS module. It overrides only the
`basic_fields_url` block of `package/snippets/resource_form.html` (via
`{% ckan_extends %}`), appending a small progress-bar/cancel widget next to
the existing native controls. When the upload finishes, it publishes CKAN
core's own `resource:uploaded` sandbox event, which `resource-form.js`
(already loaded on every resource form) uses to populate the `url`,
`url_type`, `name`, `format`, etc. fields — the same mechanism a classic
upload's post-save page state would use.

Because only that one block is touched, this composes correctly with other
extensions that override different blocks of the same template (e.g.
`ckanext-spatialdata`'s `metadata_fields` block), regardless of plugin load
order.

## Installation

```bash
pip install -e .
```

Add `s3_multipart_upload` to `ckan.plugins` in your ini file, after
`s3filestore`:

```ini
ckan.plugins = ... s3filestore s3_multipart_upload
```

### Building the frontend bundle

The committed `ckanext/s3_multipart_upload/assets/js/upload.js` is a built
artifact (esbuild bundling `@uppy/core` + `@uppy/aws-s3`), checked into git
so there's no JS build step at deploy time. To rebuild after changing
`frontend/src/upload.js`:

```bash
cd frontend
npm install
npm run build
```

## Configuration

All bucket/credentials config is inherited from `ckanext.s3filestore.*`.
This extension adds two of its own, both optional:

| Option | Default | Description |
|---|---|---|
| `ckanext.s3_multipart_upload.part_size_mb` | `25` | Target size per S3 part. Auto-scaled up if the file is large enough that this size would exceed S3's 10,000-part-per-upload limit. S3 requires every non-final part to be at least 5MB. |
| `ckanext.s3_multipart_upload.max_file_size_mb` | `5000` | Client-side upload size cap. |

## Infrastructure prerequisites (must be applied to the bucket out-of-band)

None of these are things this extension can configure for you at runtime —
they're one-time bucket/IAM setup, typically via Terraform or the console.

### 1. IAM permissions

Add to whatever policy the `ckanext.s3filestore.aws_access_key_id` /
`aws_secret_access_key` (or instance role, if `aws_use_ami_role`) already
grants:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:CreateMultipartUpload",
        "s3:UploadPart",
        "s3:CompleteMultipartUpload",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/*"
    }
  ]
}
```

### 2. Bucket CORS

The browser talks to S3 directly, so the bucket needs CORS allowing the
CKAN site's origin. `ExposeHeaders: ["ETag"]` is not optional — the browser
must be able to read the `ETag` response header for each uploaded part in
order to complete the multipart upload; S3 does not return it in the
response body.

```json
[
  {
    "AllowedOrigins": ["https://your-ckan-site.example.org"],
    "AllowedMethods": ["PUT"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

### 3. Lifecycle rules (orphan cleanup)

- **Abandoned mid-upload** (tab closed before all parts finished): use S3's
  native `AbortIncompleteMultipartUpload` lifecycle action. Needs no
  application code or extra IAM permissions — S3 does this itself.

```json
{
  "Rules": [
    {
      "ID": "abort-incomplete-multipart-uploads",
      "Status": "Enabled",
      "Filter": {"Prefix": ""},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
    }
  ]
}
```

There used to be a second rule + a `ckan-status=pending` tagging mechanism
for uploads whose form was never saved. **Removed** — on Cloudflare R2,
`DeleteObjectTagging` returns a deceptive `204` but actually deletes the
object outright, tagged or not, which was silently destroying newly
uploaded files. Confirmed by reproduction. Not needed for cleanup anyway;
the lifecycle rule above already handles orphans.

## Known limitations

- `size` and `mimetype` on the created resource come from an S3
  `head_object` call made right after `CompleteMultipartUpload` (so they're
  authoritative, not just trusted from the browser) — but only for uploads
  that go through this widget. `last_modified` is set to the browser's
  current time at upload-completion, same semantics as (but computed
  independently from) the classic upload path's server-side timestamp.
- No cross-page-reload upload resume: if the page is reloaded mid-upload,
  the in-progress multipart upload is abandoned (cleaned up by the
  `AbortIncompleteMultipartUpload` lifecycle rule above) and the user must
  pick the file again.
- One file per resource, matching the existing resource form's model — this
  does not add multi-file/bulk upload.

## Testing

```bash
pip install -e .
pip install -r dev-requirements.txt
pytest --ckan-ini=test.ini ckanext/s3_multipart_upload/tests
```

Tests use an in-process `moto` S3 mock (no external moto server or real AWS
account needed), following the same `ckan_config`/`clean_db` fixture
conventions as `ckanext-s3filestore`'s own test suite. `test.ini` sets
`ckanext.s3filestore.check_access_on_startup = false` since no real bucket
exists until a per-test mock creates one.

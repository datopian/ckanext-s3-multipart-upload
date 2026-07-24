import { Uppy } from '@uppy/core'
import AwsS3Multipart from '@uppy/aws-s3'

var MAX_S3_PARTS = 10000

function randomUUID () {
  if (window.crypto && window.crypto.randomUUID) {
    return window.crypto.randomUUID()
  }
  var bytes = new Uint8Array(16)
  window.crypto.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  var hex = Array.prototype.map.call(bytes, function (b) {
    return ('0' + b.toString(16)).slice(-2)
  }).join('')
  return (
    hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' +
    hex.slice(16, 20) + '-' + hex.slice(20)
  )
}

function guessFormat (filename) {
  var idx = filename.lastIndexOf('.')
  if (idx === -1 || idx === filename.length - 1) {
    return null
  }
  return filename.slice(idx + 1)
}

window.ckan.module('s3-multipart-upload', function ($) {
  return {
    options: {
      start_url: '',
      sign_part_url: '',
      complete_url: '',
      abort_url: '',
      mode: 'create',
      part_size_mb: 25,
      max_file_size_mb: 5000
    },

    initialize: function () {
      $.proxyAll(this, /_on/)

      this.form = this.el[0].closest('form')
      this.fileInput = this.form.querySelector('#field-resource-upload')
      this.idInput = this.form.querySelector('input[name="id"]')
      this.nameInput = this.form.querySelector('input[name="name"]')
      this.urlInput = this.form.querySelector('#field-resource-url')

      this.progressEl = this.el[0].querySelector('.s3-multipart-upload-progress')
      this.barEl = this.el[0].querySelector('.s3-multipart-upload-bar')
      this.statusEl = this.el[0].querySelector('.s3-multipart-upload-status')
      this.cancelBtn = this.el[0].querySelector('.s3-multipart-upload-cancel')
      this.errorEl = this.el[0].querySelector('.s3-multipart-upload-error')

      this._nameIsDirty = !!(this.nameInput && this.nameInput.value)
      this._originalFileInputName = this.fileInput
        ? this.fileInput.getAttribute('name')
        : null

      if (this.nameInput) {
        this.nameInput.addEventListener('change', this._onNameChanged)
      }
      if (this.fileInput) {
        this.fileInput.addEventListener('change', this._onFileChosen)
      }
      if (this.cancelBtn) {
        this.cancelBtn.addEventListener('click', this._onCancel)
      }
    },

    teardown: function () {
      if (this._uppy) {
        this._uppy.cancelAll()
      }
    },

    _onNameChanged: function () {
      this._nameIsDirty = true
    },

    _onFileChosen: function () {
      var file = this.fileInput.files && this.fileInput.files[0]
      if (!file) {
        return
      }

      this._hideError()

      var maxBytes = (this.options.max_file_size_mb || 0) * 1024 * 1024
      if (maxBytes && file.size > maxBytes) {
        this._showError(
          this._('File is too large (max {size} MB).')
            .replace('{size}', this.options.max_file_size_mb)
        )
        this.fileInput.value = ''
        return
      }

      // The file goes straight to S3 from here, never through the CKAN
      // form POST.
      this.fileInput.removeAttribute('name')

      // #field-resource-url is type="url" for the Link widget. Once we're
      // uploading, `resource:uploaded` below sets its value to a bare
      // filename (CKAN's own upload-mode convention) rather than an
      // absolute URL, which fails native type="url" constraint validation.
      // The field is hidden while Upload mode is selected, so Chromium/
      // Firefox silently refuse to submit the form instead of showing a
      // validation bubble — switch it to a plain text field, matching what
      // CKAN's own resource-upload-field.js does for the edit-mode case.
      if (this.urlInput && this.urlInput.type === 'url') {
        this.urlInput.type = 'text'
      }

      if (this.options.mode === 'create' && this.idInput && !this.idInput.value) {
        this.idInput.value = randomUUID()
      }
      this.resourceId = this.idInput ? this.idInput.value : null
      if (!this.resourceId) {
        this._showError(
          this._('Could not determine a resource id for this upload.'))
        return
      }

      this._startUpload(file)
    },

    _onCancel: function () {
      if (this._uppy) {
        this._uppy.cancelAll()
      }
      this._setUploading(false)
      this._hideProgress()
      this._resetFileInput()
    },

    _resetFileInput: function () {
      if (!this.fileInput) {
        return
      }
      this.fileInput.value = ''
      if (this._originalFileInputName) {
        this.fileInput.setAttribute('name', this._originalFileInputName)
      }
      if (this.urlInput) {
        this.urlInput.type = 'url'
      }
    },

    _csrfToken: function () {
      var input = this.form.querySelector('input[name="_csrf_token"]')
      return input ? input.value : ''
    },

    _post: function (url, body) {
      var self = this
      return fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': self._csrfToken()
        },
        body: JSON.stringify(body)
      }).then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () {
            return {}
          }).then(function (data) {
            throw new Error(
              data.error || ('Request to ' + url + ' failed (' + resp.status + ')'))
          })
        }
        return resp.json()
      })
    },

    _startUpload: function (file) {
      var self = this
      var startedFilename = null
      var completeMeta = {}

      var uppy = new Uppy({
        autoProceed: false,
        restrictions: { maxNumberOfFiles: 1 }
      })

      uppy.use(AwsS3Multipart, {
        limit: 6,
        // Default is `file.size > 100MB` - anything smaller falls back to a
        // single-PUT path via `getUploadParameters`, which we don't
        // implement (no backend endpoint for it either - every upload here
        // goes through the multipart start/sign_part/complete/abort
        // endpoints). S3 multipart supports a single-part upload fine, so
        // force every file through the one code path we actually have.
        shouldUseMultipart: true,
        getChunkSize: function (uppyFile) {
          var configured = Math.max(self.options.part_size_mb || 25, 5) * 1024 * 1024
          var minForPartLimit = Math.ceil(uppyFile.size / MAX_S3_PARTS)
          return Math.max(configured, minForPartLimit)
        },
        createMultipartUpload: function (uppyFile) {
          return self._post(self.options.start_url, {
            resource_id: self.resourceId,
            filename: uppyFile.name,
            content_type: uppyFile.type || 'application/octet-stream',
            mode: self.options.mode
          }).then(function (data) {
            startedFilename = data.filename
            return { uploadId: data.uploadId, key: data.key }
          })
        },
        listParts: function () {
          // No cross-page-load resume support: every upload starts fresh.
          return Promise.resolve([])
        },
        signPart: function (uppyFile, opts) {
          return self._post(self.options.sign_part_url, {
            resource_id: self.resourceId,
            filename: uppyFile.name,
            key: opts.key,
            upload_id: opts.uploadId,
            part_number: opts.partNumber,
            mode: self.options.mode
          }).then(function (data) {
            return { url: data.url }
          })
        },
        completeMultipartUpload: function (uppyFile, opts) {
          return self._post(self.options.complete_url, {
            resource_id: self.resourceId,
            filename: uppyFile.name,
            key: opts.key,
            upload_id: opts.uploadId,
            parts: opts.parts,
            mode: self.options.mode
          }).then(function (data) {
            startedFilename = data.filename || startedFilename
            completeMeta.size = data.size
            completeMeta.mimetype = data.mimetype
            return {}
          })
        },
        abortMultipartUpload: function (uppyFile, opts) {
          return self._post(self.options.abort_url, {
            resource_id: self.resourceId,
            filename: uppyFile.name,
            key: opts.key,
            upload_id: opts.uploadId,
            mode: self.options.mode
          }).then(function () {})
        }
      })

      this._uppy = uppy
      this._setUploading(true)
      this._showProgress()

      uppy.on('upload-progress', function (uppyFile, progress) {
        self._updateProgress(progress)
      })

      uppy.on('upload-error', function (uppyFile, error) {
        self._setUploading(false)
        self._hideProgress()
        self._showError(
          (error && error.message) || self._('Upload failed.'))
        self._resetFileInput()
      })

      uppy.addFile(file)

      uppy.upload().then(function (result) {
        if (!result || (result.failed && result.failed.length)) {
          // upload-error already reported/reset the UI for this case.
          return
        }
        self._setUploading(false)
        self._onUploadComplete(startedFilename, completeMeta)
      }).catch(function (err) {
        self._setUploading(false)
        self._hideProgress()
        self._showError((err && err.message) || self._('Upload failed.'))
        self._resetFileInput()
      })
    },

    _onUploadComplete: function (filename, completeMeta) {
      var payload = {
        url: filename,
        url_type: 'upload'
      }

      var format = guessFormat(filename)
      if (format) {
        payload.format = format
      }
      if (!this._nameIsDirty) {
        payload.name = filename
      }
      if (completeMeta && completeMeta.size != null) {
        payload.size = completeMeta.size
      }
      if (completeMeta && completeMeta.mimetype) {
        payload.mimetype = completeMeta.mimetype
      }
      payload.last_modified = new Date().toISOString()

      var uploadedMB = completeMeta && completeMeta.size != null
        ? (completeMeta.size / (1024 * 1024)).toFixed(1)
        : null
      this._showCompleted(uploadedMB)

      this.sandbox.publish('resource:uploaded', payload)
    },

    _setUploading: function (isUploading) {
      this.uploading = isUploading
      var buttons = this.form.querySelectorAll('.form-actions button[type="submit"]')
      buttons.forEach(function (btn) {
        btn.disabled = isUploading
      })
    },

    // Shown while a part is actively transferring: bar + Cancel button.
    _showProgress: function () {
      if (this.progressEl) {
        this.progressEl.classList.remove('is-complete')
        this.progressEl.style.display = ''
      }
      if (this.cancelBtn) {
        this.cancelBtn.style.display = ''
      }
    },

    // Shown once the upload finishes, and left visible (unlike the
    // in-progress state, this is not hidden again) so a user who navigates
    // away and back can still tell the upload completed rather than
    // wondering whether it silently failed.
    _showCompleted: function (uploadedMB) {
      if (this.progressEl) {
        this.progressEl.classList.add('is-complete')
        this.progressEl.style.display = ''
      }
      if (this.barEl) {
        this.barEl.style.width = '100%'
      }
      if (this.statusEl) {
        this.statusEl.textContent = this._('Upload complete') +
          (uploadedMB != null ? ' — ' + uploadedMB + ' MB' : '')
      }
      if (this.cancelBtn) {
        this.cancelBtn.style.display = 'none'
      }
    },

    _updateProgress: function (progress) {
      if (!this.barEl) {
        return
      }
      // Uppy's raw 'upload-progress' event only carries
      // {uploadStarted, bytesUploaded, bytesTotal} - no `.percentage`. That
      // field is computed by Uppy's own internal listener on the same
      // event and stored in ITS state, never added to the event object we
      // receive here, so it must be computed the same way ourselves.
      var bytesUploaded = progress.bytesUploaded || 0
      var bytesTotal = progress.bytesTotal
      var pct = (bytesTotal != null && bytesTotal > 0)
        ? Math.round((bytesUploaded / bytesTotal) * 100)
        : 0
      this.barEl.style.width = pct + '%'
      var uploadedMB = (bytesUploaded / (1024 * 1024)).toFixed(1)
      var totalMB = bytesTotal
        ? (bytesTotal / (1024 * 1024)).toFixed(1)
        : '?'
      this.statusEl.textContent = pct + '% — ' + uploadedMB + ' / ' + totalMB + ' MB'
    },

    _hideProgress: function () {
      if (this.progressEl) {
        this.progressEl.classList.remove('is-complete')
        this.progressEl.style.display = 'none'
      }
    },

    _showError: function (message) {
      if (!this.errorEl) {
        return
      }
      this.errorEl.textContent = message
      this.errorEl.style.display = ''
    },

    _hideError: function () {
      if (!this.errorEl) {
        return
      }
      this.errorEl.style.display = 'none'
      this.errorEl.textContent = ''
    }
  }
})

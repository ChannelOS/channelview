"""
ChannelView - Video Storage Service
Pluggable storage backend: local filesystem or S3-compatible (AWS S3, MinIO, Cloudflare R2).
"""
import os
import uuid
import time
import hmac
import hashlib
import logging
from datetime import datetime, timedelta
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logger = logging.getLogger('channelview.storage')


class LocalStorage:
    """Store videos on the local filesystem (development default)."""

    def __init__(self, upload_dir, intro_dir=None):
        self.upload_dir = upload_dir
        self.intro_dir = intro_dir or upload_dir
        os.makedirs(self.upload_dir, exist_ok=True)
        os.makedirs(self.intro_dir, exist_ok=True)

    def save_video(self, file_obj, candidate_id, question_id):
        """Save a video file. Returns (relative_path, file_size)."""
        filename = f"{candidate_id}_{question_id}_{int(time.time())}.webm"
        filepath = os.path.join(self.upload_dir, filename)
        file_obj.save(filepath)
        file_size = os.path.getsize(filepath)
        relative_path = f'/static/uploads/videos/{filename}'
        logger.info(f'Local: saved {filename} ({file_size} bytes)')
        return relative_path, file_size

    def save_intro(self, file_obj, user_id, original_filename):
        """Save an intro video. Returns relative_path."""
        ext = os.path.splitext(original_filename)[1] or '.webm'
        filename = f"intro_{user_id}_{int(time.time())}{ext}"
        filepath = os.path.join(self.intro_dir, filename)
        file_obj.save(filepath)
        return f'/static/uploads/intros/{filename}'

    def delete_file(self, relative_path):
        """Delete a file by its relative path."""
        if not relative_path:
            return
        # Convert relative path to absolute
        base = os.path.dirname(os.path.dirname(self.upload_dir))  # static/
        filepath = os.path.join(base, relative_path.lstrip('/').replace('static/', '', 1))
        # Safer: reconstruct from upload dirs
        filename = os.path.basename(relative_path)
        for d in [self.upload_dir, self.intro_dir]:
            fp = os.path.join(d, filename)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    logger.info(f'Local: deleted {filename}')
                except Exception as e:
                    logger.warning(f'Local: could not delete {filename}: {e}')
                return

    def get_url(self, relative_path):
        """Get URL for a file (just return the relative path for local)."""
        return relative_path

    def get_stats(self):
        """Get storage statistics."""
        total_size = 0
        file_count = 0
        for d in [self.upload_dir, self.intro_dir]:
            if os.path.exists(d):
                for f in os.listdir(d):
                    fp = os.path.join(d, f)
                    if os.path.isfile(fp):
                        total_size += os.path.getsize(fp)
                        file_count += 1
        return {
            'backend': 'local',
            'disk_files': file_count,
            'disk_usage': total_size,
            'disk_usage_mb': round(total_size / (1024 * 1024), 2)
        }


class S3Storage:
    """Store videos in S3-compatible storage (AWS S3, MinIO, Cloudflare R2)."""

    def __init__(self, bucket, region='us-east-1', access_key='', secret_key='',
                 endpoint='', prefix='videos/', presign_expiry=3600):
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.endpoint = endpoint.rstrip('/') if endpoint else f'https://s3.{region}.amazonaws.com'
        self.prefix = prefix
        self.presign_expiry = presign_expiry

    def _sign_v4(self, method, key, content_type='application/octet-stream', payload_hash='UNSIGNED-PAYLOAD'):
        """Generate AWS Signature V4 headers for S3 requests."""
        now = datetime.utcnow()
        date_stamp = now.strftime('%Y%m%d')
        amz_date = now.strftime('%Y%m%dT%H%M%SZ')
        credential_scope = f'{date_stamp}/{self.region}/s3/aws4_request'

        host = self.endpoint.replace('https://', '').replace('http://', '')
        canonical_uri = f'/{self.bucket}/{key}'

        headers_to_sign = {
            'host': host,
            'x-amz-content-sha256': payload_hash,
            'x-amz-date': amz_date,
        }
        if content_type and method == 'PUT':
            headers_to_sign['content-type'] = content_type

        signed_headers = ';'.join(sorted(headers_to_sign.keys()))
        canonical_headers = ''.join(f'{k}:{v}\n' for k, v in sorted(headers_to_sign.items()))

        canonical_request = f'{method}\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}'
        string_to_sign = f'AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode()).hexdigest()}'

        def sign(key, msg):
            return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

        k_date = sign(f'AWS4{self.secret_key}'.encode('utf-8'), date_stamp)
        k_region = sign(k_date, self.region)
        k_service = sign(k_region, 's3')
        k_signing = sign(k_service, 'aws4_request')
        signature = hmac.new(k_signing, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

        auth = f'AWS4-HMAC-SHA256 Credential={self.access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}'

        return {
            'Authorization': auth,
            'x-amz-content-sha256': payload_hash,
            'x-amz-date': amz_date,
            'Host': host,
        }

    def save_video(self, file_obj, candidate_id, question_id):
        """Upload video to S3. Returns (s3_key, file_size)."""
        filename = f"{candidate_id}_{question_id}_{int(time.time())}.webm"
        key = f"{self.prefix}{filename}"

        # Read file content
        file_obj.seek(0)
        data = file_obj.read()
        file_size = len(data)
        payload_hash = hashlib.sha256(data).hexdigest()

        url = f'{self.endpoint}/{self.bucket}/{key}'
        headers = self._sign_v4('PUT', key, 'video/webm', payload_hash)
        headers['Content-Type'] = 'video/webm'

        req = Request(url, data=data, method='PUT')
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            resp = urlopen(req, timeout=60)
            if resp.getcode() in (200, 201):
                logger.info(f'S3: uploaded {key} ({file_size} bytes)')
                return f's3://{self.bucket}/{key}', file_size
            return None, 0
        except (HTTPError, URLError) as e:
            logger.error(f'S3 upload failed: {e}')
            raise Exception(f'S3 upload failed: {e}')

    def save_intro(self, file_obj, user_id, original_filename):
        """Upload intro video to S3. Returns s3_key."""
        ext = os.path.splitext(original_filename)[1] or '.webm'
        filename = f"intro_{user_id}_{int(time.time())}{ext}"
        key = f"intros/{filename}"

        file_obj.seek(0)
        data = file_obj.read()
        payload_hash = hashlib.sha256(data).hexdigest()
        content_type = 'video/webm' if ext == '.webm' else 'video/mp4'

        url = f'{self.endpoint}/{self.bucket}/{key}'
        headers = self._sign_v4('PUT', key, content_type, payload_hash)
        headers['Content-Type'] = content_type

        req = Request(url, data=data, method='PUT')
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            resp = urlopen(req, timeout=60)
            if resp.getcode() in (200, 201):
                return f's3://{self.bucket}/{key}'
            return None
        except (HTTPError, URLError) as e:
            logger.error(f'S3 intro upload failed: {e}')
            raise Exception(f'S3 intro upload failed: {e}')

    def delete_file(self, s3_path):
        """Delete a file from S3."""
        if not s3_path or not s3_path.startswith('s3://'):
            return
        key = s3_path.split('/', 3)[-1] if '/' in s3_path[5:] else ''
        if not key:
            return

        url = f'{self.endpoint}/{self.bucket}/{key}'
        headers = self._sign_v4('DELETE', key)

        req = Request(url, method='DELETE')
        for k, v in headers.items():
            req.add_header(k, v)

        try:
            urlopen(req, timeout=15)
            logger.info(f'S3: deleted {key}')
        except Exception as e:
            logger.warning(f'S3 delete failed for {key}: {e}')

    def get_url(self, s3_path):
        """Generate a pre-signed URL for an S3 object."""
        if not s3_path or not s3_path.startswith('s3://'):
            return s3_path  # Not an S3 path, return as-is

        # Extract key from s3://bucket/key
        parts = s3_path[5:].split('/', 1)
        if len(parts) < 2:
            return s3_path
        key = parts[1]

        # Generate pre-signed URL using query string auth
        now = datetime.utcnow()
        date_stamp = now.strftime('%Y%m%d')
        amz_date = now.strftime('%Y%m%dT%H%M%SZ')
        credential_scope = f'{date_stamp}/{self.region}/s3/aws4_request'
        credential = f'{self.access_key}/{credential_scope}'

        host = self.endpoint.replace('https://', '').replace('http://', '')
        canonical_uri = f'/{self.bucket}/{key}'

        query_params = (
            f'X-Amz-Algorithm=AWS4-HMAC-SHA256'
            f'&X-Amz-Credential={quote(credential, safe="")}'
            f'&X-Amz-Date={amz_date}'
            f'&X-Amz-Expires={self.presign_expiry}'
            f'&X-Amz-SignedHeaders=host'
        )

        canonical_request = f'GET\n{canonical_uri}\n{query_params}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD'
        string_to_sign = f'AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode()).hexdigest()}'

        def sign(k, msg):
            return hmac.new(k, msg.encode('utf-8'), hashlib.sha256).digest()

        k_date = sign(f'AWS4{self.secret_key}'.encode('utf-8'), date_stamp)
        k_region = sign(k_date, self.region)
        k_service = sign(k_region, 's3')
        k_signing = sign(k_service, 'aws4_request')
        signature = hmac.new(k_signing, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

        return f'{self.endpoint}/{self.bucket}/{key}?{query_params}&X-Amz-Signature={signature}'

    def get_stats(self):
        """Get storage statistics (basic info, no listing)."""
        return {
            'backend': 's3',
            'bucket': self.bucket,
            'region': self.region,
            'prefix': self.prefix,
        }


# ======================== FACTORY ========================

def create_storage(config=None):
    """Create a storage backend based on configuration.

    Args:
        config: Config object with STORAGE_BACKEND, S3_*, UPLOAD_DIR, INTRO_DIR attributes.
                If None, creates local storage with defaults.
    """
    if config is None:
        base = os.path.dirname(__file__)
        return LocalStorage(
            upload_dir=os.path.join(base, 'static', 'uploads', 'videos'),
            intro_dir=os.path.join(base, 'static', 'uploads', 'intros')
        )

    backend = getattr(config, 'STORAGE_BACKEND', 'local')

    if backend == 's3':
        return S3Storage(
            bucket=config.S3_BUCKET,
            region=config.S3_REGION,
            access_key=config.S3_ACCESS_KEY,
            secret_key=config.S3_SECRET_KEY,
            endpoint=config.S3_ENDPOINT,
            prefix=config.S3_PREFIX,
            presign_expiry=config.S3_PRESIGN_EXPIRY,
        )

    return LocalStorage(
        upload_dir=getattr(config, 'UPLOAD_DIR', os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'videos')),
        intro_dir=getattr(config, 'INTRO_DIR', os.path.join(os.path.dirname(__file__), 'static', 'uploads', 'intros'))
    )

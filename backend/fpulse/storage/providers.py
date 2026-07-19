"""
Storage Providers for F-Pulse backup & recovery.

F-Pulse: Local filesystem (SQLite + local files), with optional cloud
destinations — S3, Azure Blob, GCS, MinIO.

Principle: F-Pulse is a TOOL. User owns all data. We store NOTHING.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class StorageProvider(ABC):
    """Abstract storage provider for backup/restore operations."""

    @abstractmethod
    def upload(self, local_path: str, remote_key: str) -> bool:
        """Upload a local file to remote storage."""

    @abstractmethod
    def download(self, remote_key: str, local_path: str) -> bool:
        """Download a remote file to local path."""

    @abstractmethod
    def list_backups(self) -> list[dict]:
        """List available backups with metadata."""

    @abstractmethod
    def delete(self, remote_key: str) -> bool:
        """Delete a remote file."""

    @abstractmethod
    def test_connection(self) -> dict:
        """Test connectivity to the storage provider."""


class LocalStorageProvider(StorageProvider):
    """Local filesystem backup — default for F-Pulse Free."""

    def __init__(self, backup_dir: str | None = None):
        self.backup_dir = backup_dir or os.path.join(
            os.environ.get("FPULSE_DATA_DIR", "data"), "backups"
        )
        os.makedirs(self.backup_dir, exist_ok=True)

    def upload(self, local_path: str, remote_key: str) -> bool:
        dest = os.path.join(self.backup_dir, remote_key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(local_path, dest)
        logger.info("Local backup: %s -> %s", local_path, dest)
        return True

    def download(self, remote_key: str, local_path: str) -> bool:
        src = os.path.join(self.backup_dir, remote_key)
        if not os.path.isfile(src):
            return False
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        shutil.copy2(src, local_path)
        return True

    def list_backups(self) -> list[dict]:
        backups = []
        if not os.path.isdir(self.backup_dir):
            return backups
        for f in sorted(os.listdir(self.backup_dir), reverse=True):
            fpath = os.path.join(self.backup_dir, f)
            if os.path.isfile(fpath):
                stat = os.stat(fpath)
                backups.append({
                    "key": f,
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "provider": "local",
                })
        return backups

    def delete(self, remote_key: str) -> bool:
        fpath = os.path.join(self.backup_dir, remote_key)
        if os.path.isfile(fpath):
            os.remove(fpath)
            return True
        return False

    def test_connection(self) -> dict:
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            test_file = os.path.join(self.backup_dir, ".test")
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
            return {"status": "ok", "message": f"Local backup dir: {self.backup_dir}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


class S3StorageProvider(StorageProvider):
    """Amazon S3 / MinIO backup — F-Pulse feature."""

    def __init__(self, bucket: str, prefix: str = "fpulse-backups/", **kwargs):
        self.bucket = bucket
        self.prefix = prefix
        self.endpoint_url = kwargs.get("endpoint_url")  # For MinIO
        self.region = kwargs.get("region", "us-east-1")
        self.access_key = kwargs.get("access_key")
        self.secret_key = kwargs.get("secret_key")
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3
            kwargs = {"region_name": self.region}
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            if self.access_key and self.secret_key:
                kwargs["aws_access_key_id"] = self.access_key
                kwargs["aws_secret_access_key"] = self.secret_key
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def upload(self, local_path: str, remote_key: str) -> bool:
        try:
            client = self._get_client()
            client.upload_file(local_path, self.bucket, f"{self.prefix}{remote_key}")
            logger.info("S3 upload: %s -> s3://%s/%s%s", local_path, self.bucket, self.prefix, remote_key)
            return True
        except Exception as e:
            logger.error("S3 upload failed: %s", e)
            return False

    def download(self, remote_key: str, local_path: str) -> bool:
        try:
            client = self._get_client()
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            client.download_file(self.bucket, f"{self.prefix}{remote_key}", local_path)
            return True
        except Exception as e:
            logger.error("S3 download failed: %s", e)
            return False

    def list_backups(self) -> list[dict]:
        try:
            client = self._get_client()
            resp = client.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix)
            backups = []
            for obj in resp.get("Contents", []):
                key = obj["Key"].replace(self.prefix, "", 1)
                if key:
                    backups.append({
                        "key": key,
                        "size_bytes": obj["Size"],
                        "created_at": obj["LastModified"].isoformat(),
                        "provider": "s3",
                    })
            return sorted(backups, key=lambda x: x["created_at"], reverse=True)
        except Exception as e:
            logger.error("S3 list failed: %s", e)
            return []

    def delete(self, remote_key: str) -> bool:
        try:
            client = self._get_client()
            client.delete_object(Bucket=self.bucket, Key=f"{self.prefix}{remote_key}")
            return True
        except Exception as e:
            logger.error("S3 delete failed: %s", e)
            return False

    def test_connection(self) -> dict:
        try:
            client = self._get_client()
            client.head_bucket(Bucket=self.bucket)
            return {"status": "ok", "message": f"S3 bucket accessible: {self.bucket}"}
        except Exception as e:
            return {"status": "error", "message": f"S3 connection failed: {e}"}


class AzureBlobStorageProvider(StorageProvider):
    """Azure Blob Storage backup — F-Pulse feature."""

    def __init__(self, container: str, prefix: str = "fpulse-backups/", **kwargs):
        self.container = container
        self.prefix = prefix
        self.connection_string = kwargs.get("connection_string")
        self.account_name = kwargs.get("account_name")
        self.account_key = kwargs.get("account_key")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from azure.storage.blob import ContainerClient
            if self.connection_string:
                self._client = ContainerClient.from_connection_string(
                    self.connection_string, self.container
                )
            else:
                from azure.storage.blob import BlobServiceClient
                url = f"https://{self.account_name}.blob.core.windows.net"
                service = BlobServiceClient(url, credential=self.account_key)
                self._client = service.get_container_client(self.container)
        return self._client

    def upload(self, local_path: str, remote_key: str) -> bool:
        try:
            client = self._get_client()
            with open(local_path, "rb") as f:
                client.upload_blob(f"{self.prefix}{remote_key}", f, overwrite=True)
            logger.info("Azure upload: %s -> %s/%s%s", local_path, self.container, self.prefix, remote_key)
            return True
        except Exception as e:
            logger.error("Azure upload failed: %s", e)
            return False

    def download(self, remote_key: str, local_path: str) -> bool:
        try:
            client = self._get_client()
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            blob = client.download_blob(f"{self.prefix}{remote_key}")
            with open(local_path, "wb") as f:
                blob.readinto(f)
            return True
        except Exception as e:
            logger.error("Azure download failed: %s", e)
            return False

    def list_backups(self) -> list[dict]:
        try:
            client = self._get_client()
            backups = []
            for blob in client.list_blobs(name_starts_with=self.prefix):
                key = blob.name.replace(self.prefix, "", 1)
                if key:
                    backups.append({
                        "key": key,
                        "size_bytes": blob.size,
                        "created_at": blob.last_modified.isoformat() if blob.last_modified else "",
                        "provider": "azure_blob",
                    })
            return sorted(backups, key=lambda x: x["created_at"], reverse=True)
        except Exception as e:
            logger.error("Azure list failed: %s", e)
            return []

    def delete(self, remote_key: str) -> bool:
        try:
            client = self._get_client()
            client.delete_blob(f"{self.prefix}{remote_key}")
            return True
        except Exception as e:
            logger.error("Azure delete failed: %s", e)
            return False

    def test_connection(self) -> dict:
        try:
            client = self._get_client()
            client.get_container_properties()
            return {"status": "ok", "message": f"Azure container accessible: {self.container}"}
        except Exception as e:
            return {"status": "error", "message": f"Azure connection failed: {e}"}


class GCSStorageProvider(StorageProvider):
    """Google Cloud Storage backup — F-Pulse feature."""

    def __init__(self, bucket: str, prefix: str = "fpulse-backups/", **kwargs):
        self.bucket_name = bucket
        self.prefix = prefix
        self.credentials_path = kwargs.get("credentials_path")
        self._bucket = None

    def _get_bucket(self):
        if self._bucket is None:
            from google.cloud import storage
            if self.credentials_path:
                client = storage.Client.from_service_account_json(self.credentials_path)
            else:
                client = storage.Client()
            self._bucket = client.bucket(self.bucket_name)
        return self._bucket

    def upload(self, local_path: str, remote_key: str) -> bool:
        try:
            bucket = self._get_bucket()
            blob = bucket.blob(f"{self.prefix}{remote_key}")
            blob.upload_from_filename(local_path)
            logger.info("GCS upload: %s -> gs://%s/%s%s", local_path, self.bucket_name, self.prefix, remote_key)
            return True
        except Exception as e:
            logger.error("GCS upload failed: %s", e)
            return False

    def download(self, remote_key: str, local_path: str) -> bool:
        try:
            bucket = self._get_bucket()
            blob = bucket.blob(f"{self.prefix}{remote_key}")
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            blob.download_to_filename(local_path)
            return True
        except Exception as e:
            logger.error("GCS download failed: %s", e)
            return False

    def list_backups(self) -> list[dict]:
        try:
            bucket = self._get_bucket()
            backups = []
            for blob in bucket.list_blobs(prefix=self.prefix):
                key = blob.name.replace(self.prefix, "", 1)
                if key:
                    backups.append({
                        "key": key,
                        "size_bytes": blob.size or 0,
                        "created_at": blob.time_created.isoformat() if blob.time_created else "",
                        "provider": "gcs",
                    })
            return sorted(backups, key=lambda x: x["created_at"], reverse=True)
        except Exception as e:
            logger.error("GCS list failed: %s", e)
            return []

    def delete(self, remote_key: str) -> bool:
        try:
            bucket = self._get_bucket()
            blob = bucket.blob(f"{self.prefix}{remote_key}")
            blob.delete()
            return True
        except Exception as e:
            logger.error("GCS delete failed: %s", e)
            return False

    def test_connection(self) -> dict:
        try:
            bucket = self._get_bucket()
            bucket.exists()
            return {"status": "ok", "message": f"GCS bucket accessible: {self.bucket_name}"}
        except Exception as e:
            return {"status": "error", "message": f"GCS connection failed: {e}"}


def create_provider(config: dict) -> StorageProvider:
    """Factory: create a storage provider from config.

    Config example:
        {"provider": "local"}
        {"provider": "s3", "bucket": "my-backups", "region": "us-east-1"}
        {"provider": "azure_blob", "container": "backups", "connection_string": "..."}
        {"provider": "gcs", "bucket": "my-backups", "credentials_path": "/path/to/sa.json"}
        {"provider": "minio", "bucket": "backups", "endpoint_url": "http://minio:9000", ...}
    """
    provider_type = config.get("provider", "local")

    if provider_type == "local":
        return LocalStorageProvider(backup_dir=config.get("backup_dir"))

    elif provider_type in ("s3", "minio"):
        return S3StorageProvider(
            bucket=config["bucket"],
            prefix=config.get("prefix", "fpulse-backups/"),
            endpoint_url=config.get("endpoint_url"),
            region=config.get("region", "us-east-1"),
            access_key=config.get("access_key"),
            secret_key=config.get("secret_key"),
        )

    elif provider_type == "azure_blob":
        return AzureBlobStorageProvider(
            container=config["container"],
            prefix=config.get("prefix", "fpulse-backups/"),
            connection_string=config.get("connection_string"),
            account_name=config.get("account_name"),
            account_key=config.get("account_key"),
        )

    elif provider_type == "gcs":
        return GCSStorageProvider(
            bucket=config["bucket"],
            prefix=config.get("prefix", "fpulse-backups/"),
            credentials_path=config.get("credentials_path"),
        )

    else:
        raise ValueError(f"Unknown storage provider: {provider_type}")

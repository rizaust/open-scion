"""Content-verified client API for native workspaces and importers."""

from ..store import ScionError, blob_id
from .transport import HTTPTransport


class Client(HTTPTransport):
    """Verify immutable object identities at the transport boundary."""

    def put_blob(self, data: bytes) -> str:
        oid = self.request("POST", "/v1/blobs", body=data)["id"]
        if oid != blob_id(data):
            raise ScionError("Server returned an incorrect blob ID", code="integrity", status=502)
        return oid

    def get_blob(self, oid: str) -> bytes:
        if len(oid) != 64 or any(c not in "0123456789abcdef" for c in oid):
            raise ScionError("Invalid blob ID")
        data = self.request("GET", "/v1/blobs/" + oid, raw=True)
        if blob_id(data) != oid:
            raise ScionError("Downloaded blob failed content verification", code="integrity", status=502)
        return data

"""Solution package SBOM, provenance, signing, and trust verification."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field

from sage.digests import canonical_digest, directory_digest, file_digest

if TYPE_CHECKING:
    from sage.solution_packages import SolutionBundleManifest

_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SupplyChainError(ValueError):
    """Raised when Solution supply-chain evidence cannot be trusted."""


class SolutionProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    builder_id: str = Field(alias="builderId", min_length=1)
    source_uri: str = Field(alias="sourceUri", min_length=1)
    source_digest: str = Field(alias="sourceDigest", pattern=r"^sha256:[0-9a-f]{64}$")
    build_type: str = Field(alias="buildType", min_length=1)


class SolutionSignature(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    algorithm: str
    key_id: str = Field(alias="keyId")
    payload_digest: str = Field(alias="payloadDigest", pattern=r"^sha256:[0-9a-f]{64}$")
    signature: str


class SolutionPackageVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    package_digest: str
    manifest_digest: str
    sbom_digest: str | None = None
    provenance_digest: str | None = None
    signature_key_id: str | None = None
    signature_verified: bool = False

    def release_metadata(self) -> dict[str, str]:
        values = self.model_dump(mode="json", exclude_none=True)
        return {
            key: str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in values.items()
        }


class SolutionTrustStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root.resolve() if root is not None else None

    def verify(
        self,
        package_root: Path,
        manifest: SolutionBundleManifest,
        *,
        signature_required: bool,
    ) -> SolutionPackageVerification:
        manifest_path = package_root / "solution.yaml"
        sbom_path = package_root / "sbom.json"
        provenance_path = package_root / "provenance.json"
        signature_path = package_root / "signature.json"
        sbom_digest = self._validate_sbom(sbom_path) if sbom_path.is_file() else None
        provenance_digest = (
            self._validate_provenance(provenance_path)
            if provenance_path.is_file()
            else None
        )
        payload_digest = _signature_payload_digest(
            manifest_path=manifest_path,
            manifest=manifest,
            sbom_digest=sbom_digest,
            provenance_digest=provenance_digest,
        )
        key_id = None
        verified = False
        if signature_path.is_file():
            signature = SolutionSignature.model_validate_json(
                signature_path.read_text(encoding="utf-8")
            )
            if signature.algorithm != "ed25519":
                raise SupplyChainError("Unsupported Solution signature algorithm")
            if signature.payload_digest != payload_digest:
                raise SupplyChainError("Solution signature payload digest mismatch")
            public_key = self._load_public_key(signature.key_id)
            try:
                public_key.verify(
                    base64.b64decode(signature.signature, validate=True),
                    payload_digest.encode("ascii"),
                )
            except Exception as exc:
                raise SupplyChainError("Solution signature verification failed") from exc
            key_id = signature.key_id
            verified = True
        elif signature_required:
            raise SupplyChainError("A trusted Solution package signature is required")
        return SolutionPackageVerification(
            package_digest=directory_digest(package_root),
            manifest_digest=file_digest(manifest_path),
            sbom_digest=sbom_digest,
            provenance_digest=provenance_digest,
            signature_key_id=key_id,
            signature_verified=verified,
        )

    def _load_public_key(self, key_id: str) -> Ed25519PublicKey:
        if self._root is None or not _KEY_ID.fullmatch(key_id):
            raise SupplyChainError("Solution signature key is not trusted")
        path = (self._root / f"{key_id}.pem").resolve()
        if not path.is_relative_to(self._root) or not path.is_file():
            raise SupplyChainError("Solution signature key is not trusted")
        key = serialization.load_pem_public_key(path.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise SupplyChainError("Solution trust key must be Ed25519")
        return key

    @staticmethod
    def _validate_sbom(path: Path) -> str:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SupplyChainError("Invalid Solution SBOM") from exc
        if not isinstance(value, dict) or value.get("bomFormat") != "CycloneDX":
            raise SupplyChainError("Solution SBOM must use CycloneDX")
        if not isinstance(value.get("specVersion"), str) or not isinstance(
            value.get("components", []),
            list,
        ):
            raise SupplyChainError("Solution SBOM is incomplete")
        return file_digest(path)

    @staticmethod
    def _validate_provenance(path: Path) -> str:
        try:
            SolutionProvenance.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SupplyChainError("Invalid Solution provenance") from exc
        return file_digest(path)


def sign_solution_package(
    package_root: Path,
    *,
    private_key_path: Path,
    key_id: str,
) -> Path:
    if not _KEY_ID.fullmatch(key_id):
        raise SupplyChainError("Invalid Solution signing key id")
    from sage.solution_packages import validate_solution_bundle_directory

    manifest = validate_solution_bundle_directory(package_root)
    trust_store = SolutionTrustStore()
    sbom_path = package_root / "sbom.json"
    provenance_path = package_root / "provenance.json"
    sbom_digest = trust_store._validate_sbom(sbom_path) if sbom_path.is_file() else None
    provenance_digest = (
        trust_store._validate_provenance(provenance_path)
        if provenance_path.is_file()
        else None
    )
    payload_digest = _signature_payload_digest(
        manifest_path=package_root / "solution.yaml",
        manifest=manifest,
        sbom_digest=sbom_digest,
        provenance_digest=provenance_digest,
    )
    key = serialization.load_pem_private_key(
        private_key_path.read_bytes(),
        password=None,
    )
    if not isinstance(key, Ed25519PrivateKey):
        raise SupplyChainError("Solution signing key must be Ed25519")
    signature = SolutionSignature(
        algorithm="ed25519",
        keyId=key_id,
        payloadDigest=payload_digest,
        signature=base64.b64encode(key.sign(payload_digest.encode("ascii"))).decode("ascii"),
    )
    path = package_root / "signature.json"
    path.write_text(signature.model_dump_json(by_alias=True, indent=2), encoding="utf-8")
    return path


def _signature_payload_digest(
    *,
    manifest_path: Path,
    manifest: SolutionBundleManifest,
    sbom_digest: str | None,
    provenance_digest: str | None,
) -> str:
    return canonical_digest(
        {
            "manifest_digest": file_digest(manifest_path),
            "solution_id": manifest.metadata.name,
            "version": manifest.metadata.version,
            "components": [
                component.model_dump(mode="json", by_alias=True)
                for component in manifest.spec.components
            ],
            "sbom_digest": sbom_digest,
            "provenance_digest": provenance_digest,
        }
    )
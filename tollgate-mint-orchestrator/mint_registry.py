import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class MintEntry:
    npub: str
    hex_pubkey: str
    subdomain: str
    url: str
    rest_port: int
    grpc_port: int
    container_name: str
    created_at: str
    max_single_issuance: int = 10000
    max_balance: int = 1000000
    units: str = "sat"


class MintRegistry:
    def __init__(self, path: str):
        self.path = path
        self.mints: list[MintEntry] = []

    @classmethod
    def load(cls, path: str) -> "MintRegistry":
        registry = cls(path)
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
            for m in data.get("mints", []):
                registry.mints.append(MintEntry(**m))
        return registry

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {"mints": [asdict(m) for m in self.mints]}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def get_mint_by_url(self, url: str) -> Optional[MintEntry]:
        for m in self.mints:
            if m.url == url:
                return m
        return None

    def get_mint_by_subdomain(self, subdomain: str) -> Optional[MintEntry]:
        for m in self.mints:
            if m.subdomain == subdomain:
                return m
        return None

    def get_mint_by_hex_pubkey(self, hex_pubkey: str) -> Optional[MintEntry]:
        for m in self.mints:
            if m.hex_pubkey == hex_pubkey:
                return m
        return None

    def add_mint(self, mint_data: dict):
        entry = MintEntry(**mint_data)
        existing = self.get_mint_by_subdomain(entry.subdomain)
        if existing:
            self.mints.remove(existing)
        self.mints.append(entry)
        self.save()

    def remove_mint(self, subdomain: str):
        self.mints = [m for m in self.mints if m.subdomain != subdomain]
        self.save()

    def list_mints(self) -> list[MintEntry]:
        return list(self.mints)

    @staticmethod
    def derive_subdomain(npub: str) -> str:
        return npub[5:17]

    @staticmethod
    def next_ports(mints: list["MintEntry"]) -> tuple[int, int]:
        if not mints:
            return (3338, 50051)
        max_rest = max(m.rest_port for m in mints)
        max_grpc = max(m.grpc_port for m in mints)
        return (max_rest + 1, max_grpc + 1)

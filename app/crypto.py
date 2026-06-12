import os
from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.environ["TOKEN_ENCRYPTION_KEY"]
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def generate_key() -> str:
    """Helper to generate a valid Fernet key (run once, store in env)."""
    return Fernet.generate_key().decode()

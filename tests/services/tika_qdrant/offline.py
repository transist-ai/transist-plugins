"""Run actual model inference without a network, including missing-asset failure."""

import asyncio
import shutil
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

from provider import FILES, ProvisionedProvider


async def validate() -> None:
    """Assert local inference and fail-closed behavior for each required asset."""
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=2)
    except OSError:
        pass
    else:
        raise AssertionError("Offline container has external connectivity")
    with patch(
        "socket.socket.connect", side_effect=AssertionError("Download attempted")
    ):
        provider = ProvisionedProvider()
        vector = await provider.embed_query(
            "How should I calibrate a temperature sensor?"
        )
    assert len(vector) == 384
    assert provider.get_vector_name() == "fast-all-minilm-l6-v2"
    for missing in FILES:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for source in Path("/assets/model").iterdir():
                if source.name != missing:
                    (target / source.name).symlink_to(source)
            try:
                ProvisionedProvider(target)
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("Missing asset did not fail closed")
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        shutil.copy("/assets/model/manifest.json", target)
        try:
            ProvisionedProvider(target)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("Missing snapshot did not fail closed")
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        for source in Path("/assets/model").iterdir():
            (target / source.name).symlink_to(source)
        (target / "config.json").unlink()
        (target / "config.json").write_text("corrupt fixture asset")
        try:
            ProvisionedProvider(target)
        except ValueError:
            pass
        else:
            raise AssertionError("Corrupt asset did not fail closed")
    print("PASS: real offline inference; all six missing-asset cases fail closed")


if __name__ == "__main__":
    asyncio.run(validate())

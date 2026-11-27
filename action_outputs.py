"""Write action outputs without interpreting API messages as runner commands."""

from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4


def write_outputs(env: Mapping[str, str], values: Mapping[str, str]) -> None:
    output = env.get("GITHUB_OUTPUT", "")
    if not output:
        return
    with Path(output).open("a", encoding="utf-8") as stream:
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                delimiter = f"coverage_{uuid4().hex}"
                stream.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                stream.write(f"{name}={value}\n")

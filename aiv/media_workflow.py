"""Bounded media jobs sharing the text campaign's physical account ledger."""

from dataclasses import replace
import base64
import hashlib
import json
import mimetypes
from pathlib import Path
import time

from openai import OpenAI

from .pool import CallError, Pool, canonical


def media_config(base, models):
    """Use the same per-key account names as full_turn_workflow."""
    accounts = {}
    credentials = []
    for c in base.credentials:
        if c.provider != "next":
            continue
        group = c.alias + "_account"
        accounts[group] = replace(base.accounts[c.group], name=group,
                                  concurrency=4, call_limit=10_000_000,
                                  token_limit=10**12)
        credentials.append(replace(c, group=group, models=tuple(models)))
    return replace(base, credentials=credentials, accounts=accounts,
                   concurrency=32, max_attempts=2, timeout=120)


def media_transport(credential, task, timeout):
    spec = json.loads(task["messages"][-1]["content"])
    kind = spec["kind"]
    client = OpenAI(api_key=credential.key, base_url=credential.base_url,
                    timeout=timeout, max_retries=0)
    try:
        if kind == "generate_image":
            response = client.images.generate(model=task["model"], prompt=spec["prompt"],
                                              size=spec["size"], n=1)
            data = response.data[0]
            encoded = getattr(data, "b64_json", None)
            if not encoded:
                return {"text": "", "valid": False, "validation_code": "no_image_payload"}
            binary = base64.b64decode(encoded, validate=True)
            if not binary.startswith(b"\x89PNG\r\n\x1a\n") and not binary.startswith(b"\xff\xd8"):
                return {"text": "", "valid": False, "validation_code": "unexpected_image_format"}
            suffix = ".png" if binary.startswith(b"\x89PNG") else ".jpg"
            output_dir = Path(spec["output_dir"]).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(canonical([task["model"], spec["prompt"], spec["size"]]).encode()).hexdigest()
            dest = output_dir / (name + suffix)
            temporary = dest.with_suffix(dest.suffix + ".tmp")
            temporary.write_bytes(binary)
            temporary.replace(dest)
            result = {"kind": kind, "model": task["model"], "path": str(dest),
                      "sha256": hashlib.sha256(binary).hexdigest(), "bytes": len(binary)}
            return {"text": canonical(result), "valid": True,
                    "input_tokens": None, "output_tokens": None,
                    "finish_reason": "image_generated"}
        if kind == "analyze_image":
            path = Path(spec["image_path"]).resolve()
            binary = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0]
            if mime not in ("image/png", "image/jpeg", "image/webp"):
                raise ValueError("Unsupported image MIME")
            encoded = base64.b64encode(binary).decode("ascii")
            response = client.chat.completions.create(
                model=task["model"],
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": spec["prompt"]},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ]}], max_tokens=task["output_limit"])
            answer = response.choices[0].message.content or ""
            return {"text": answer, "valid": bool(answer),
                    "input_tokens": getattr(response.usage, "prompt_tokens", None),
                    "output_tokens": getattr(response.usage, "completion_tokens", None),
                    "finish_reason": response.choices[0].finish_reason}
        raise ValueError("Unsupported media kind")
    except Exception as error:
        if isinstance(error, ValueError):
            raise CallError("invalid_request") from None
        status = getattr(error, "status_code", None)
        if status in (401, 403):
            kind = "authentication"
        elif status in (402,):
            kind = "exhausted"
        elif status == 429:
            kind = "rate_limit"
        elif status == 408 or "timeout" in type(error).__name__.lower():
            kind = "timeout"
        elif status in (409, 500, 502, 503, 504, 524) or status is None:
            kind = "temporary"
        else:
            kind = "invalid_request"
        raise CallError(kind, status=status) from None


def enqueue_media(pool: Pool, *, kind, model, prompt, experiment, replicate,
                  output_dir=None, image_path=None, size="1024x1024"):
    if kind not in ("generate_image", "analyze_image"):
        raise ValueError("Unsupported media kind")
    if not prompt.strip():
        raise ValueError("Empty media prompt")
    spec = {"kind": kind, "prompt": prompt}
    if kind == "generate_image":
        if not output_dir:
            raise ValueError("Output directory required")
        spec.update(output_dir=str(Path(output_dir).resolve()), size=size)
    else:
        if not image_path or not Path(image_path).is_file():
            raise ValueError("Existing image path required")
        spec["image_path"] = str(Path(image_path).resolve())
        spec["image_sha256"] = hashlib.sha256(Path(image_path).read_bytes()).hexdigest()
    return pool.enqueue(provider="next", model=model, experiment=experiment,
                        replicate=replicate, messages=[{"role": "user", "content": canonical(spec)}],
                        output_limit=1024 if kind == "analyze_image" else 8192)

#!/usr/bin/env python3
"""Real encoder qualification; an HTTP health response alone is insufficient."""

import argparse
import json
import math
import os
import pathlib
import time
import urllib.error
import urllib.request


def validate(base_url, model="bge-m3-local-v1"):
    headers = {"Content-Type": "application/json"}
    if key := os.environ.get("EMBEDDING_TEST_API_KEY"):
        headers["Authorization"] = "Bearer " + key

    def embed(texts):
        body = json.dumps({"model": model, "input": texts, "encoding_format": "float"}).encode()
        request = urllib.request.Request(base_url.rstrip("/") + "/embeddings", data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
        assert payload["model"] == model, "Wrong serving model"
        rows = sorted(payload["data"], key=lambda row: row["index"])
        assert [row["index"] for row in rows] == list(range(len(texts))), "Wrong input association"
        vectors = [row["embedding"] for row in rows]
        for vector in vectors:
            assert len(vector) == 1024, "Wrong dimensions"
            assert all(math.isfinite(value) for value in vector), "Non-finite vector"
            assert abs(math.sqrt(sum(value * value for value in vector)) - 1) < 1e-4, "Not normalized"
        return vectors

    texts = [
        "When must payment be made?",
        "The customer must pay the invoice within thirty days of receipt.",
        "The forest is home to owls and wild deer.",
        "Le paiement est exigible trente jours après réception de la facture.",
    ]
    start = time.monotonic()
    vectors = embed(texts)
    similarities = [sum(a * b for a, b in zip(vectors[0], other)) for other in vectors[1:]]
    assert similarities[0] > similarities[1] + .1, "Semantic retrieval failed"
    assert similarities[2] > similarities[1] + .1, "Multilingual retrieval failed"
    repeated = embed([texts[0]])[0]
    assert max(abs(a - b) for a, b in zip(vectors[0], repeated)) < 1e-5, "Unstable embedding"
    try:
        embed(["token " * 2000])
    except urllib.error.HTTPError as error:
        assert error.code in (400, 422), f"Unexpected over-limit status {error.code}"
    else:
        raise AssertionError("Overlong input was silently truncated")
    return {"model": model, "dimensions": 1024, "semantic_scores": similarities,
            "overlong_rejected": True, "deterministic": True,
            "elapsed_seconds": round(time.monotonic() - start, 3)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible base URL, including /v1")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    result = json.dumps(validate(args.base_url), indent=2) + "\n"
    if args.output:
        args.output.write_text(result)
    print(result, end="")

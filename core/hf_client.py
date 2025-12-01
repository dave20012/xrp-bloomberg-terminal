"""Hugging Face inference helper for regulatory tagging."""
from __future__ import annotations

from typing import Dict

import requests

from core.config import settings
from core.utils import logger


MODEL_ID = "finiteautomata/beto-sentiment-analysis"  # placeholder multilingual model


def classify_headline(headline: str) -> Dict[str, float]:
    if not settings.hf_token:
        logger.warning("HF_TOKEN missing; returning neutral classification.")
        return {"regulatory_threat": 0.0, "regulatory_support": 0.0, "neutral_unclear": 1.0}
    url = f"{settings.hf_inference_base}/{MODEL_ID}"
    headers = {"Authorization": f"Bearer {settings.hf_token}"}
    resp = requests.post(url, headers=headers, json={"inputs": headline}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    scores = {entry.get("label", "unknown"): entry.get("score", 0.0) for entry in data}
    return {
        "regulatory_threat": scores.get("NEG", 0.0),
        "regulatory_support": scores.get("POS", 0.0),
        "neutral_unclear": scores.get("NEU", 0.0),
    }

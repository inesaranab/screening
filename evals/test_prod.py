import json
import pathlib

import httpx
import pytest

URL = "https://screening-app.grayhill-6c021b7d.westeurope.azurecontainerapps.io/screen"

_FIXTURES = json.loads((pathlib.Path(__file__).parent / "fixtures.json").read_text())
CASES = _FIXTURES["cases"]
JOB_DESCRIPTIONS = _FIXTURES["job_descriptions"]


@pytest.mark.prod
@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_prod_screen_redacts_pii(prod_api_key, case):
    async with httpx.AsyncClient(timeout=120) as client:
        # warm the container first
        await client.get(URL.replace("/screen", "/health"))
        resp = await client.post(
            URL,
            headers={"x-api-key": prod_api_key, "content-type": "application/json"},
            json={
                "transcript": case["transcript"],
                "job_description": JOB_DESCRIPTIONS[case["jd"]],
            },
        )
        resp.raise_for_status()
        body = resp.json()
        assert "fit_score" in body["assessment"] and "rationale" in body["assessment"]
        for field, want in case.get("expect", {}).items():
            assert body["flags"][field] == want
        for leaked in case.get("must_not_leak", []):
            assert leaked not in resp.text

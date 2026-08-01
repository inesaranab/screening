import httpx
import pytest

URL = "https://screening-app.grayhill-6c021b7d.westeurope.azurecontainerapps.io/screen"
CANARY_NINO = "QQ 12 34 56 C"


@pytest.mark.prod
@pytest.mark.asyncio
async def test_prod_screen_redacts_pii(prod_api_key):
    async with httpx.AsyncClient(timeout=120) as client:
        # warm the container first
        await client.get(URL.replace("/screen", "/health"))
        resp = await client.post(
            URL,
            headers={"x-api-key": prod_api_key, "content-type": "application/json"},
            json={
                "transcript": f"5 years Python and FastAPI. My NI number is {CANARY_NINO}.",
                "job_description": "Backend engineeer, Python, FastAPI",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        assert "fit_score" in body["assessment"] and "rationale" in body["assessment"]
        assert CANARY_NINO not in resp.text

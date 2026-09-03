from fastapi import FastAPI

from homecareos.intake.router import router as intake_router

app = FastAPI(title="HomeCareOS API")

app.include_router(intake_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

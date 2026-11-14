"""觸發 Cloud Run Job（例如管理員在網頁上按「同步」）。服務帳號需具備該 Job 的 run.jobs.run 權限。"""
from __future__ import annotations

import google.auth
from google.auth.transport.requests import AuthorizedSession

from src import config

RUN_API = "https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{job}:run"


class JobTriggerError(Exception):
    """觸發背景工作失敗時拋出，訊息需可直接呈現給使用者。"""


def trigger_sync_job() -> None:
    if not (config.GCP_PROJECT and config.SYNC_JOB_NAME):
        raise JobTriggerError("GCP_PROJECT / SYNC_JOB_NAME 未設定，無法觸發同步工作。")
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    url = RUN_API.format(project=config.GCP_PROJECT, region=config.GCP_REGION, job=config.SYNC_JOB_NAME)
    resp = AuthorizedSession(creds).post(url, json={}, timeout=30)
    if resp.status_code >= 300:
        raise JobTriggerError(f"觸發同步工作失敗（HTTP {resp.status_code}）：{resp.text[:300]}")

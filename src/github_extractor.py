"""
透過 PyGithub 從目標倉庫抓取「PM 需要理解的實作事實」：
    1. README.md
    2. API 規格 / 路由定義 (openapi.json、swagger.json、routes/ 目錄等)
    3. 最近 N 筆已合併 (merged) 的 Pull Request（標題、內文、變更檔案）

回傳統一格式的結構化文件列表，供 summarizer.py 進一步轉譯為「功能卡片」。
"""
from __future__ import annotations

from typing import List, Optional

from github import Auth, Github
from github.GithubException import (
    BadCredentialsException,
    GithubException,
    RateLimitExceededException,
    UnknownObjectException,
)

from src import config
from src.documents import ExtractedDocument

logger = config.get_logger(__name__)

# 常見的 API 規格檔案路徑（依序嘗試）
CANDIDATE_API_SPEC_FILES = [
    "openapi.json",
    "openapi.yaml",
    "openapi.yml",
    "swagger.json",
    "swagger.yaml",
    "docs/openapi.json",
    "docs/openapi.yaml",
    "api/openapi.json",
]

# 常見的路由定義目錄（依序嘗試）
CANDIDATE_ROUTE_DIRS = ["routes", "src/routes", "app/routes", "api", "src/api", "app/api"]

# 掃描路由目錄時的檔案副檔名白名單，避免撈進二進位檔
ROUTE_FILE_EXTENSIONS = (".py", ".js", ".ts", ".go", ".java", ".rb")

# 掃描路由目錄的安全上限，避免超大 repo 打爆 API rate limit
MAX_ROUTE_FILES = 40
MAX_ROUTE_DEPTH = 3
MAX_FILE_BYTES = 40_000  # 單檔最多讀取的位元組數，超過會截斷


class GitHubExtractorError(Exception):
    """GitHub 存取相關的所有錯誤統一包裝成此類別，訊息需可直接呈現給使用者。"""


class GitHubExtractor:
    """封裝對單一 GitHub repo 的抓取邏輯。"""

    def __init__(
        self,
        token: Optional[str] = None,
        repo_full_name: Optional[str] = None,
    ) -> None:
        self.token = token or config.GITHUB_TOKEN
        self.repo_full_name = repo_full_name or config.GITHUB_REPO

        if not self.token:
            raise GitHubExtractorError(
                "GITHUB_TOKEN 未設定，請在 .env 中設定有效的 GitHub Personal Access Token。"
            )
        if not self.repo_full_name or "/" not in self.repo_full_name:
            raise GitHubExtractorError(
                f"GITHUB_REPO 格式錯誤：'{self.repo_full_name}'，需為 `owner/repo`。"
            )

        try:
            self._client = Github(auth=Auth.Token(self.token), timeout=30)
            self._repo = self._client.get_repo(self.repo_full_name)
        except BadCredentialsException as exc:
            raise GitHubExtractorError(
                "GitHub Token 無效或已過期，請至 GitHub 重新產生 Personal Access Token。"
            ) from exc
        except UnknownObjectException as exc:
            raise GitHubExtractorError(
                f"找不到倉庫 `{self.repo_full_name}`，請確認名稱正確、且 Token 有存取此私有倉庫的權限。"
            ) from exc
        except RateLimitExceededException as exc:
            raise GitHubExtractorError(
                "GitHub API 已達速率限制 (Rate Limit)，請稍後再試。"
            ) from exc
        except GithubException as exc:
            raise GitHubExtractorError(f"連線 GitHub 失敗：{exc}") from exc

    # ------------------------------------------------------------------
    # README
    # ------------------------------------------------------------------
    def get_readme(self) -> Optional[ExtractedDocument]:
        try:
            readme = self._repo.get_readme()
            content = readme.decoded_content.decode("utf-8", errors="replace")
            return ExtractedDocument(
                doc_type="readme",
                title=f"{self.repo_full_name} README",
                content=content,
                files=[readme.path],
                metadata={"path": readme.path},
            )
        except UnknownObjectException:
            logger.warning("倉庫 %s 沒有 README。", self.repo_full_name)
            return None
        except GithubException as exc:
            logger.warning("讀取 README 失敗：%s", exc)
            return None

    # ------------------------------------------------------------------
    # API 規格 / 路由定義
    # ------------------------------------------------------------------
    def get_api_spec_documents(self) -> List[ExtractedDocument]:
        docs: List[ExtractedDocument] = []

        # 1) 明確的規格檔案
        for path in CANDIDATE_API_SPEC_FILES:
            try:
                content_file = self._repo.get_contents(path)
                if isinstance(content_file, list):
                    continue  # 是目錄，跳過
                text = content_file.decoded_content.decode("utf-8", errors="replace")
                docs.append(
                    ExtractedDocument(
                        doc_type="api_spec",
                        title=f"API 規格：{path}",
                        content=text[:MAX_FILE_BYTES],
                        files=[path],
                        metadata={"path": path},
                    )
                )
            except UnknownObjectException:
                continue
            except GithubException as exc:
                logger.warning("讀取 %s 失敗：%s", path, exc)
                continue

        # 2) 常見路由目錄，遞迴掃描（有上限保護）
        for dir_path in CANDIDATE_ROUTE_DIRS:
            try:
                root_contents = self._repo.get_contents(dir_path)
            except UnknownObjectException:
                continue
            except GithubException as exc:
                logger.warning("讀取目錄 %s 失敗：%s", dir_path, exc)
                continue

            if not isinstance(root_contents, list):
                continue  # 不是目錄

            route_files = self._walk_route_dir(root_contents, depth=1)
            for cf in route_files:
                try:
                    text = cf.decoded_content.decode("utf-8", errors="replace")
                except Exception as exc:  # noqa: BLE001 - 二進位/解碼例外皆略過
                    logger.warning("跳過無法解碼的檔案 %s：%s", cf.path, exc)
                    continue
                docs.append(
                    ExtractedDocument(
                        doc_type="api_spec",
                        title=f"路由定義：{cf.path}",
                        content=text[:MAX_FILE_BYTES],
                        files=[cf.path],
                        metadata={"path": cf.path},
                    )
                )
            if route_files:
                break  # 找到一個有效目錄就好，避免重複抓取

        return docs

    def _walk_route_dir(self, contents: list, depth: int) -> list:
        """遞迴走訪目錄，收集符合副檔名白名單的檔案（有數量與深度上限）。"""
        collected = []
        if depth > MAX_ROUTE_DEPTH:
            return collected

        for item in contents:
            if len(collected) >= MAX_ROUTE_FILES:
                break
            try:
                if item.type == "dir":
                    try:
                        sub_contents = self._repo.get_contents(item.path)
                        if isinstance(sub_contents, list):
                            collected.extend(self._walk_route_dir(sub_contents, depth + 1))
                    except GithubException as exc:
                        logger.warning("讀取子目錄 %s 失敗：%s", item.path, exc)
                elif item.type == "file" and item.path.endswith(ROUTE_FILE_EXTENSIONS):
                    collected.append(item)
            except GithubException as exc:
                logger.warning("走訪 %s 時發生錯誤：%s", getattr(item, "path", "?"), exc)

        return collected[:MAX_ROUTE_FILES]

    # ------------------------------------------------------------------
    # 已合併 PR
    # ------------------------------------------------------------------
    def get_recent_merged_prs(self, limit: Optional[int] = None) -> List[ExtractedDocument]:
        limit = limit or config.PR_FETCH_LIMIT
        docs: List[ExtractedDocument] = []

        try:
            pulls = self._repo.get_pulls(state="closed", sort="updated", direction="desc")
        except GithubException as exc:
            raise GitHubExtractorError(f"讀取 Pull Requests 失敗：{exc}") from exc

        collected = 0
        try:
            for pr in pulls:
                if collected >= limit:
                    break
                if not pr.merged:
                    continue

                try:
                    changed_files = [f.filename for f in pr.get_files()]
                except GithubException as exc:
                    logger.warning("讀取 PR #%s 變更檔案失敗：%s", pr.number, exc)
                    changed_files = []

                body = (pr.body or "").strip() or "(PR 未填寫說明)"

                docs.append(
                    ExtractedDocument(
                        doc_type="pr",
                        title=f"PR #{pr.number}: {pr.title}",
                        content=body,
                        files=changed_files,
                        metadata={
                            "number": pr.number,
                            "author": pr.user.login if pr.user else "unknown",
                            "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                            "url": pr.html_url,
                        },
                    )
                )
                collected += 1
        except RateLimitExceededException as exc:
            logger.warning("讀取 PR 途中達到速率限制，已回傳目前抓到的 %d 筆。", collected)
            if not docs:
                raise GitHubExtractorError("GitHub API 已達速率限制，請稍後再試。") from exc
        except GithubException as exc:
            raise GitHubExtractorError(f"讀取 Pull Requests 失敗：{exc}") from exc

        return docs

    # ------------------------------------------------------------------
    # 一次抓齊所有來源
    # ------------------------------------------------------------------
    def extract_all(self, pr_limit: Optional[int] = None) -> List[ExtractedDocument]:
        """回傳 README + API 規格 + 最近 merged PR 的統一文件列表。"""
        docs: List[ExtractedDocument] = []

        readme = self.get_readme()
        if readme:
            docs.append(readme)

        docs.extend(self.get_api_spec_documents())
        docs.extend(self.get_recent_merged_prs(limit=pr_limit))

        logger.info("從 %s 抓取到 %d 份文件。", self.repo_full_name, len(docs))
        return docs

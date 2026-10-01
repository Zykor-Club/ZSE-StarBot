# -*- coding: utf-8 -*-
"""
GitHub REST API 封装（公开仓库/组织，无需 Token）
只读接口：仓库统计、最新 Issue / PR、组织仓库列表
"""

import aiohttp

API_BASE = "https://api.github.com"
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "ZSE-Bot",
}

# 可选 Token（配置在 config.yaml 的 github.token）。
# 2026-07 起 GitHub 限制 stargazers 列表接口必须鉴权，有 Token 才能拿到"新增 star 的用户名"。
GH_TOKEN = None


class GitHubError(Exception):
    """GitHub API 请求失败"""


def _headers(star_accept: bool = False) -> dict:
    h = dict(API_HEADERS)
    if GH_TOKEN:
        h["Authorization"] = f"Bearer {GH_TOKEN}"
    if star_accept:
        h["Accept"] = "application/vnd.github.star+json"
    return h


async def _fetch(session: aiohttp.ClientSession, path: str, params=None, star_accept: bool = False):
    url = f"{API_BASE}{path}"
    async with session.get(url, params=params, headers=_headers(star_accept)) as r:
        if r.status == 200:
            return await r.json()
        text = await r.text()
        raise GitHubError(f"GitHub API {r.status} {path}: {text[:200]}")


async def get_repo_stats(session, owner: str, repo: str) -> dict:
    """仓库统计：star/fork/issue 等"""
    data = await _fetch(session, f"/repos/{owner}/{repo}")
    return {
        "full_name": f"{owner}/{repo}",
        "name": repo,
        "description": data.get("description") or "",
        "stars": data.get("stargazers_count", 0),
        "forks": data.get("forks_count", 0),
        "watchers": data.get("subscribers_count", 0),
        "open_issues": data.get("open_issues_count", 0),
        "language": data.get("language") or "未知",
        "pushed_at": data.get("pushed_at", ""),
        "html_url": data.get("html_url", "") or f"https://github.com/{owner}/{repo}",
    }


async def get_latest_pulls(session, owner: str, repo: str, count: int = 5) -> list:
    """最近提交的 PR（含 open/closed/merged）"""
    data = await _fetch(
        session,
        f"/repos/{owner}/{repo}/pulls",
        {"state": "all", "sort": "created", "direction": "desc", "per_page": count},
    )
    return [
        {
            "number": p["number"],
            "title": p.get("title", ""),
            "user": (p.get("user") or {}).get("login", "?"),
            "state": p.get("state", ""),  # open / closed
            "merged_at": p.get("merged_at"),  # 非空表示已合并
            "created_at": p.get("created_at", ""),
            "html_url": p.get("html_url", ""),
        }
        for p in data
    ]


async def get_latest_issues(session, owner: str, repo: str, count: int = 5) -> list:
    """最近提交的 Issue（issues 端点会混入 PR，这里过滤掉）"""
    data = await _fetch(
        session,
        f"/repos/{owner}/{repo}/issues",
        {"state": "all", "sort": "created", "direction": "desc", "per_page": count},
    )
    return [
        {
            "number": i["number"],
            "title": i.get("title", ""),
            "user": (i.get("user") or {}).get("login", "?"),
            "state": i.get("state", ""),
            "created_at": i.get("created_at", ""),
            "html_url": i.get("html_url", ""),
        }
        for i in data
        if "pull_request" not in i  # 排除混在 issue 列表里的 PR
    ]


async def get_org_repos(session, org: str) -> list:
    """组织下的公开仓库列表"""
    data = await _fetch(session, f"/orgs/{org}/repos", {"per_page": 100})
    return [
        {
            "full_name": r["full_name"],
            "name": r["name"],
            "stars": r.get("stargazers_count", 0),
            "forks": r.get("forks_count", 0),
            "open_issues": r.get("open_issues_count", 0),
            "description": r.get("description") or "",
        }
        for r in data
    ]


async def get_repo_stargazers(session, owner: str, repo: str, count: int = 20) -> list:
    """
    最近 star 的用户列表（含时间）。
    注意：2026-07 起该接口仅管理员/协作者可访问（需 config 里的 github.token 鉴权），
    无 Token 或权限不足会抛 GitHubError。
    """
    data = await _fetch(
        session,
        f"/repos/{owner}/{repo}/stargazers",
        {"per_page": count},
        star_accept=True,
    )
    return [
        {
            "login": (d.get("user") or {}).get("login", "?"),
            "starred_at": d.get("starred_at", ""),
        }
        for d in data
    ]
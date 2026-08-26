from __future__ import annotations

import calendar
import datetime as dt
import html
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

USERNAME = os.getenv("USER_NAME", "amorim-guiz")
BIRTHDAY = dt.date(2003, 8, 14)
TOKEN = os.getenv("ACCESS_TOKEN") or os.getenv("GITHUB_TOKEN")
GRAPHQL_URL = "https://api.github.com/graphql"

CACHE_FILE = Path("cache/profile_stats.json")
SVG_FILES = [Path("dark_mode.svg"), Path("light_mode.svg")]
AFFILIATIONS_ALL = ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]


def graphql(query: str, variables: dict) -> dict:
    if not TOKEN:
        raise RuntimeError("GitHub token not found. Set ACCESS_TOKEN or GITHUB_TOKEN.")

    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": f"{USERNAME}-profile-updater",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub GraphQL HTTP {exc.code}: {body}") from exc

    if result.get("errors"):
        raise RuntimeError("GitHub GraphQL error:\n" + json.dumps(result["errors"], indent=2))

    return result["data"]


def plural(value: int, word: str) -> str:
    return word if value == 1 else word + "s"


def add_months(date_value: dt.date, months: int) -> dt.date:
    month_index = date_value.month - 1 + months
    year = date_value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(date_value.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def uptime_string(today: dt.date | None = None) -> str:
    today = today or dt.date.today()

    years = today.year - BIRTHDAY.year
    if add_months(BIRTHDAY, years * 12) > today:
        years -= 1

    year_anchor = add_months(BIRTHDAY, years * 12)
    months = (today.year - year_anchor.year) * 12 + today.month - year_anchor.month
    month_anchor = add_months(year_anchor, months)

    if month_anchor > today:
        months -= 1
        month_anchor = add_months(year_anchor, months)

    days = (today - month_anchor).days

    return (
        f"{years} {plural(years, 'year')}, "
        f"{months} {plural(months, 'month')}, "
        f"{days} {plural(days, 'day')}"
    )


def get_followers() -> int:
    query = """
    query($login: String!) {
      user(login: $login) {
        followers { totalCount }
      }
    }
    """
    data = graphql(query, {"login": USERNAME})
    return int(data["user"]["followers"]["totalCount"])


def get_repositories(affiliations: list[str]) -> list[dict]:
    query = """
    query($login: String!, $affiliations: [RepositoryAffiliation!], $cursor: String) {
      user(login: $login) {
        repositories(
          first: 100,
          after: $cursor,
          ownerAffiliations: $affiliations,
          orderBy: {field: UPDATED_AT, direction: DESC}
        ) {
          nodes {
            name
            nameWithOwner
            owner { login }
            stargazerCount
            defaultBranchRef {
              target {
                ... on Commit {
                  history { totalCount }
                }
              }
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
    }
    """

    repos = []
    cursor = None

    while True:
        data = graphql(
            query,
            {"login": USERNAME, "affiliations": affiliations, "cursor": cursor},
        )
        connection = data["user"]["repositories"]
        repos.extend(connection["nodes"])

        if not connection["pageInfo"]["hasNextPage"]:
            return repos

        cursor = connection["pageInfo"]["endCursor"]


def repository_commit_stats(owner: str, name: str) -> dict:
    query = """
    query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor) {
                nodes {
                  additions
                  deletions
                  author {
                    user { login }
                  }
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
          }
        }
      }
    }
    """

    commits = additions = deletions = 0
    cursor = None

    while True:
        data = graphql(query, {"owner": owner, "name": name, "cursor": cursor})
        repository = data.get("repository")

        if not repository or not repository.get("defaultBranchRef"):
            return {"commits": 0, "additions": 0, "deletions": 0}

        history = repository["defaultBranchRef"]["target"]["history"]

        for commit in history["nodes"]:
            author = commit.get("author") or {}
            user = author.get("user") or {}
            login = user.get("login")

            if login and login.lower() == USERNAME.lower():
                commits += 1
                additions += int(commit["additions"])
                deletions += int(commit["deletions"])

        if not history["pageInfo"]["hasNextPage"]:
            return {
                "commits": commits,
                "additions": additions,
                "deletions": deletions,
            }

        cursor = history["pageInfo"]["endCursor"]


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {"repositories": {}}

    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"repositories": {}}


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def collect_code_stats(repositories: list[dict]) -> dict:
    old_cache = load_cache().get("repositories", {})
    new_cache = {}

    total_commits = 0
    total_additions = 0
    total_deletions = 0

    for repository in repositories:
        full_name = repository["nameWithOwner"]
        owner, name = full_name.split("/", 1)

        branch = repository.get("defaultBranchRef")
        branch_commit_count = 0
        if branch and branch.get("target"):
            branch_commit_count = int(branch["target"]["history"]["totalCount"])

        cached = old_cache.get(full_name)

        if cached and int(cached.get("branch_commit_count", -1)) == branch_commit_count:
            stats = {
                "commits": int(cached.get("commits", 0)),
                "additions": int(cached.get("additions", 0)),
                "deletions": int(cached.get("deletions", 0)),
            }
            print(f"[cache] {full_name}")
        else:
            print(f"[scan ] {full_name}")
            stats = repository_commit_stats(owner, name)

        new_cache[full_name] = {
            "branch_commit_count": branch_commit_count,
            **stats,
        }

        total_commits += stats["commits"]
        total_additions += stats["additions"]
        total_deletions += stats["deletions"]

    save_cache(
        {
            "username": USERNAME,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "repositories": new_cache,
        }
    )

    return {
        "commits": total_commits,
        "additions": total_additions,
        "deletions": total_deletions,
        "net": total_additions - total_deletions,
    }


def format_number(value: int) -> str:
    return f"{value:,}"


def replace_text_by_id(svg: str, element_id: str, value: str) -> str:
    safe_value = html.escape(value, quote=False)
    pattern = re.compile(
        rf'(<text\b[^>]*\bid="{re.escape(element_id)}"[^>]*>)(.*?)(</text>)',
        flags=re.DOTALL,
    )

    updated, count = pattern.subn(
        lambda match: match.group(1) + safe_value + match.group(3),
        svg,
        count=1,
    )

    if count == 0:
        print(f"[warn ] SVG id not found: {element_id}")

    return updated


def update_svg(path: Path, values: dict[str, str]) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        print(f"[skip ] {path} does not exist or is empty")
        return False

    original = path.read_text(encoding="utf-8")
    updated = original

    for element_id, value in values.items():
        updated = replace_text_by_id(updated, element_id, value)

    if updated == original:
        print(f"[same ] {path}")
        return False

    path.write_text(updated, encoding="utf-8")
    print(f"[write] {path}")
    return True


def main() -> None:
    print(f"Updating profile for @{USERNAME}")

    owned_repos = get_repositories(["OWNER"])
    all_repos = get_repositories(AFFILIATIONS_ALL)

    owned_count = len(owned_repos)
    contributed_count = sum(
        1 for repo in all_repos
        if repo["owner"]["login"].lower() != USERNAME.lower()
    )
    stars = sum(int(repo["stargazerCount"]) for repo in owned_repos)
    followers = get_followers()
    code = collect_code_stats(all_repos)

    values = {
        "uptime_data": uptime_string(),
        "repo_data": format_number(owned_count),
        "contrib_data": format_number(contributed_count),
        "star_data": format_number(stars),
        "commit_data": format_number(code["commits"]),
        "follower_data": format_number(followers),
        "loc_net_data": format_number(code["net"]),
        "loc_add_data": format_number(code["additions"]) + "++",
        "loc_del_data": format_number(code["deletions"]) + "--",
    }

    print("\nProfile stats")
    print("-------------")
    for key, value in values.items():
        print(f"{key:15} {value}")
    print()

    for svg_file in SVG_FILES:
        update_svg(svg_file, values)


if __name__ == "__main__":
    main()

"""
generate_stats.py

Pulls live stats for GH_USERNAME from GitHub's GraphQL API and writes them
into the <text id="..."> nodes of light_mode.svg / dark_mode.svg.

Why a cache: computing total lines-of-code means walking every commit in
every repo you've touched and summing the diffs you authored. Doing that
in full on every run is slow and can trip GitHub's secondary rate limits.
So each repo's commit total is checked cheaply first; the expensive walk
only re-runs for a repo when its commit count has changed since last time.

Env vars required:
  GH_USERNAME    e.g. "memoowi"
  ACCESS_TOKEN   a PAT (classic or fine-grained) with read access to your
                 repos/followers/contributions. The default Actions token
                 is scoped to a single repo and isn't enough for this.
"""

import json
import os
import sys
import time
import datetime
from pathlib import Path

import requests
from dateutil.relativedelta import relativedelta
from lxml import etree

GH_USERNAME = os.environ["GH_USERNAME"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
HEADERS = {"Authorization": f"bearer {ACCESS_TOKEN}"}
API_URL = "https://api.github.com/graphql"

CACHE_PATH = Path(__file__).parent / "cache" / f"{GH_USERNAME}.json"
MAX_PAGES_PER_REPO_PER_RUN = 10  # 100 commits/page -> up to 1000 commits/repo/run


def gql(query, variables):
    resp = requests.post(API_URL, json={"query": query, "variables": variables}, headers=HEADERS)
    if resp.status_code != 200:
        raise RuntimeError(f"GraphQL call failed ({resp.status_code}): {resp.text[:300]}")
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def get_user():
    query = """
    query($login: String!) {
      user(login: $login) {
        id
        createdAt
        followers { totalCount }
      }
    }"""
    data = gql(query, {"login": GH_USERNAME})["user"]
    return data["id"], data["createdAt"], data["followers"]["totalCount"]


def get_repos(owner_id):
    """Repos owned by the user: name, star count, default-branch commit total."""
    repos = []
    cursor = None
    query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 50, after: $cursor, ownerAffiliations: [OWNER], isFork: false) {
          pageInfo { hasNextPage endCursor }
          edges {
            node {
              nameWithOwner
              stargazerCount
              defaultBranchRef {
                target {
                  ... on Commit { history { totalCount } }
                }
              }
            }
          }
        }
      }
    }"""
    while True:
        data = gql(query, {"login": GH_USERNAME, "cursor": cursor})["user"]["repositories"]
        for edge in data["edges"]:
            node = edge["node"]
            branch = node["defaultBranchRef"]
            total_commits = branch["target"]["history"]["totalCount"] if branch else 0
            repos.append({
                "name": node["nameWithOwner"],
                "stars": node["stargazerCount"],
                "commit_total": total_commits,
            })
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
    return repos


def walk_repo_commits(owner, repo, author_id):
    """Paginate this repo's commit history, summing diffs authored by author_id."""
    additions = deletions = my_commits = 0
    cursor = None
    query = """
    query($owner: String!, $repo: String!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                edges {
                  node { additions deletions author { user { id } } }
                }
              }
            }
          }
        }
      }
    }"""
    for _ in range(MAX_PAGES_PER_REPO_PER_RUN):
        data = gql(query, {"owner": owner, "repo": repo, "cursor": cursor})["repository"]
        branch = data["defaultBranchRef"]
        if branch is None:
            break
        history = branch["target"]["history"]
        for edge in history["edges"]:
            node = edge["node"]
            if node["author"]["user"] and node["author"]["user"]["id"] == author_id:
                my_commits += 1
                additions += node["additions"]
                deletions += node["deletions"]
        if not history["pageInfo"]["hasNextPage"]:
            break
        cursor = history["pageInfo"]["endCursor"]
        time.sleep(0.1)  # be polite to the API
    return additions, deletions, my_commits


def load_cache():
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def save_cache(cache):
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def build_loc_and_commit_totals(repos, author_id, cache):
    """
    For each repo: reuse the cache if its commit count hasn't moved,
    otherwise re-walk it. Stops early (keeping older cached data for the
    rest) if the API starts rejecting requests, so a partial run still
    produces a usable README instead of crashing the whole job.
    """
    total_add = total_del = total_commits = 0
    for repo in repos:
        name = repo["name"]
        cached = cache.get(name)
        needs_refresh = cached is None or cached["commit_total"] != repo["commit_total"]
        if needs_refresh:
            try:
                owner, repo_name = name.split("/", 1)
                add, dele, mine = walk_repo_commits(owner, repo_name, author_id)
                cache[name] = {
                    "commit_total": repo["commit_total"],
                    "additions": add,
                    "deletions": dele,
                    "my_commits": mine,
                }
            except RuntimeError as e:
                print(f"  ! skipping refresh for {name}: {e}", file=sys.stderr)
                if cached is None:
                    continue  # nothing to fall back on, drop this repo for now
        entry = cache[name]
        total_add += entry["additions"]
        total_del += entry["deletions"]
        total_commits += entry["my_commits"]
    return total_add, total_del, total_commits


def member_since(created_at_iso):
    created = datetime.datetime.strptime(created_at_iso, "%Y-%m-%dT%H:%M:%SZ")
    diff = relativedelta(datetime.datetime.utcnow(), created)
    parts = []
    if diff.years:
        parts.append(f"{diff.years} yr{'s' if diff.years != 1 else ''}")
    if diff.months:
        parts.append(f"{diff.months} mo{'s' if diff.months != 1 else ''}")
    if not parts:
        parts.append(f"{diff.days} day{'s' if diff.days != 1 else ''}")
    return ", ".join(parts)


def fmt(n):
    return f"{n:,}"


def update_svg(path, values):
    tree = etree.parse(str(path))
    root = tree.getroot()
    for element_id, text in values.items():
        el = root.find(f".//*[@id='{element_id}']")
        if el is not None:
            el.text = text
        else:
            print(f"  ! no element with id='{element_id}' in {path.name}", file=sys.stderr)
    tree.write(str(path), xml_declaration=False)


def main():
    print(f"Fetching stats for {GH_USERNAME}...")
    author_id, created_at, followers = get_user()
    repos = get_repos(author_id)

    cache = load_cache()
    total_add, total_del, total_commits = build_loc_and_commit_totals(repos, author_id, cache)
    save_cache(cache)

    values = {
        "repo_data": fmt(len(repos)),
        "star_data": fmt(sum(r["stars"] for r in repos)),
        "commit_data": fmt(total_commits),
        "follower_data": fmt(followers),
        "loc_data": fmt(total_add - total_del),
        "loc_add": f"+{fmt(total_add)}",
        "loc_del": f"-{fmt(total_del)}",
        "uptime_data": member_since(created_at),
    }

    base_dir = Path(__file__).parent
    for svg_name in ("light_mode.svg", "dark_mode.svg"):
        update_svg(base_dir / svg_name, values)

    print("Updated SVGs with:")
    for k, v in values.items():
        print(f"  {k:14} {v}")


if __name__ == "__main__":
    main()

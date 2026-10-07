"""P1-2: GitHub is updated after saves only, never on a timer, never with an empty commit.

The old loop pushed the whole vault every 60 s and GitHub made a commit each time,
even with identical content. Now a save signals the sync task, a reply's calls are
grouped into one commit, only changed files are uploaded, and nothing is committed
when GitHub already has the content.
"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile

import httpx
import pytest

import thread_save.export.sync_loop as sync_loop
from thread_save.config import load_config
from thread_save.export.github import GitHubDataApiTarget, GitHubFileEntry, git_blob_sha
from thread_save.service import TurnService
from thread_save.storage.writer import FileStore
from tests.test_github_export import MockGitHubApi

REPO = "testowner/testrepo"


class RealisticGitHub(MockGitHubApi):
    """Trees layer on base_tree and carry real git blob shas, as on GitHub."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/repos/{self.repo}/git/trees":
            payload = json.loads(request.content.decode("utf-8"))
            self.write_calls.append({"method": "POST", "endpoint": "/git/trees", "payload": payload})
            base = {i["path"]: dict(i) for i in self.trees.get(payload.get("base_tree"), {}).get("tree", [])}
            for item in payload.get("tree", []):
                if item.get("content") is not None:
                    base[item["path"]] = {"path": item["path"], "mode": "100644", "type": "blob",
                                          "sha": git_blob_sha(item["content"])}
                else:
                    base.pop(item["path"], None)
            self.tree_counter += 1
            new_sha = f"t{self.tree_counter:03d}_new_tree"
            self.trees[new_sha] = {"sha": new_sha, "tree": list(base.values())}
            return httpx.Response(201, json={"sha": new_sha, "tree": list(base.values())})
        return super().handle_request(request)

    def commit_messages(self) -> list[str]:
        return [c["payload"]["message"] for c in self.write_calls if c["endpoint"] == "/git/commits"]

    def uploaded_paths(self) -> list[list[str]]:
        return [[i["path"] for i in c["payload"]["tree"]] for c in self.write_calls if c["endpoint"] == "/git/trees"]


@pytest.fixture
def github(monkeypatch):
    mock = RealisticGitHub(repo=REPO)
    transport = httpx.MockTransport(mock.handle_request)

    def target(*args, **kwargs):
        kwargs["client"] = httpx.AsyncClient(transport=transport)
        return GitHubDataApiTarget(*args, **kwargs)

    monkeypatch.setattr(sync_loop, "GitHubDataApiTarget", target)
    monkeypatch.setenv("THREADVAULT_SYNC_SETTLE_SECONDS", "0.3")
    monkeypatch.setenv("THREADVAULT_SYNC_MAX_WAIT_SECONDS", "5")
    return mock, transport


@pytest.fixture
def vault():
    path = Path(tempfile.mkdtemp(prefix="tv_gh_sync_"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _service(vault: Path):
    cfg = replace(load_config(), vault_root=vault, default_account="tester")
    store = FileStore(cfg)
    return store, TurnService(store, config=cfg)


def _start(vault: Path, store) -> asyncio.Task:
    return asyncio.create_task(sync_loop.start_github_sync_loop(
        vault_root=vault, repo=f"https://github.com/{REPO}.git", token="ghp_test", store=store,
    ))


async def _settle(seconds: float = 0.8) -> None:
    await asyncio.sleep(seconds)


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── push_batch(skip_unchanged=True) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_identical_content_makes_no_commit(github):
    mock, transport = github
    target = GitHubDataApiTarget(repo=REPO, token="ghp_test", client=httpx.AsyncClient(transport=transport))
    files = [GitHubFileEntry(path="2026/10/a_p01.md", content="# A"),
             GitHubFileEntry(path="2026/10/b_p01.md", content="# B")]
    first = await target.push_batch(files, "first", skip_unchanged=True)
    again = await target.push_batch(files, "again", skip_unchanged=True)
    assert first.commit_sha and not again.commit_sha
    assert mock.commit_messages() == ["first"]


@pytest.mark.asyncio
async def test_only_changed_files_are_uploaded(github):
    mock, transport = github
    target = GitHubDataApiTarget(repo=REPO, token="ghp_test", client=httpx.AsyncClient(transport=transport))
    await target.push_batch([GitHubFileEntry(path="a.md", content="A"), GitHubFileEntry(path="b.md", content="B")],
                            "first", skip_unchanged=True)
    await target.push_batch([GitHubFileEntry(path="a.md", content="A"), GitHubFileEntry(path="b.md", content="B2")],
                            "second", skip_unchanged=True)
    assert mock.uploaded_paths() == [["a.md", "b.md"], ["b.md"]]


# ── the sync task ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_saves_means_no_commits(github, vault):
    mock, _ = github
    store, svc = _service(vault)
    await svc.log_turn(user_message="q1", reply="a1", title_hint="First chat")
    task = _start(vault, store)
    await _settle()
    assert len(mock.commit_messages()) == 1  # startup: GitHub did not have the page yet
    await _settle(1.2)                         # idle: nothing happens
    await _stop(task)
    assert len(mock.commit_messages()) == 1


@pytest.mark.asyncio
async def test_one_reply_with_two_calls_is_one_commit(github, vault):
    mock, _ = github
    store, svc = _service(vault)
    task = _start(vault, store)
    await _settle()
    assert mock.commit_messages() == []  # empty vault: nothing to push

    start = await svc.log_turn(user_message="pending projects?", title_hint="Odoo pending projects")
    await svc.log_turn(user_message="pending projects?", reply="## Projects\n- A",
                       thread_id=start["thread_id"], turn=start["n"])
    await _settle()
    await _stop(task)
    assert mock.commit_messages() == ["vault: update 'Odoo pending projects'"]


@pytest.mark.asyncio
async def test_a_save_that_changes_nothing_makes_no_commit(github, vault):
    mock, _ = github
    store, svc = _service(vault)
    r = await svc.log_turn(user_message="q1", reply="a1")
    task = _start(vault, store)
    await _settle()
    # Retry of the same call: nothing changes, no page is written
    await svc.log_turn(user_message="q1", reply="a1", thread_id=r["thread_id"], turn=r["n"])
    await _settle()
    await _stop(task)
    assert len(mock.commit_messages()) == 1


@pytest.mark.asyncio
async def test_restart_with_github_up_to_date_makes_no_commit(github, vault):
    mock, _ = github
    store, svc = _service(vault)
    await svc.log_turn(user_message="q1", reply="a1")
    task = _start(vault, store)
    await _settle()
    await _stop(task)
    # Server sleeps and wakes: a new process, same disk, same GitHub
    store2, _ = _service(vault)
    task = _start(vault, store2)
    await _settle()
    await _stop(task)
    assert len(mock.commit_messages()) == 1


@pytest.mark.asyncio
async def test_without_the_store_the_sync_does_not_run(github, vault):
    mock, _ = github
    await sync_loop.start_github_sync_loop(vault_root=vault, repo=REPO, token="ghp_test")
    assert mock.commit_messages() == []

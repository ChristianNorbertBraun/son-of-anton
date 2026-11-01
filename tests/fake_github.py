"""An in-memory GitHub for the adapter tests: the API paths they use, the shapes the real one returns, no network."""
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit


class FakeGitHub:
    bot = "anton[bot]"

    def __init__(self, slug="example/repo"):
        self.slug, self.issues, self.pulls, self.branches, self.labels = slug, {}, {}, {}, set()
        self.tokens, self.calls, self.fail_graphql = [], [], None
        self.next_number = 1

    def mint(self, app_id, key, installation, repo, permissions):
        self.tokens.append(dict(permissions))
        return f"token-{len(self.tokens)}"

    def api(self, method, path, token, body=None):
        self.calls.append((method, path, token))
        if path == "/graphql":
            return self.graphql(body)
        url = urlsplit(path)
        route = url.path.removeprefix(f"/repos/{self.slug}")
        query = parse_qs(url.query)
        for pattern, handler in self.ROUTES:
            m = re.fullmatch(pattern, f"{method} {route}")
            if m:
                return handler(self, query, body, *m.groups())
        raise RuntimeError(f"GitHub API {method} {path}: 404 b'no route'")

    def _issue(self, number):
        issue = self.issues.get(int(number))
        if issue is None:
            raise RuntimeError(f"GitHub API GET /issues/{number}: 404 b'Not Found'")
        return issue

    def _pull(self, number):
        pull = self.pulls.get(int(number))
        if pull is None:
            raise RuntimeError(f"GitHub API GET /pulls/{number}: 404 b'Not Found'")
        return pull

    @staticmethod
    def _page(items, query):
        size, page = int(query.get("per_page", ["30"])[0]), int(query.get("page", ["1"])[0])
        return items[(page - 1) * size:page * size]

    def list_issues(self, query, body):
        label = query["labels"][0]
        return [i for i in self.issues.values()
                if i["state"] == "open" and any(x["name"] == label for x in i["labels"])]

    def get_issue(self, query, body, number):
        return self._issue(number)

    def events(self, query, body, number):
        return self._page(self._issue(number).get("_events", []), query)

    def add_labels(self, query, body, number):
        issue = self._issue(number)
        for name in body["labels"]:
            if not any(x["name"] == name for x in issue["labels"]):
                issue["labels"].append({"name": name})
        return []

    def remove_label(self, query, body, number, label):
        issue, name = self._issue(number), unquote(label)
        if not any(x["name"] == name for x in issue["labels"]):
            raise RuntimeError(f"GitHub API DELETE label: 404 b'Label does not exist'")
        issue["labels"] = [x for x in issue["labels"] if x["name"] != name]
        return []

    def list_comments(self, query, body, number):
        return self._page(self._issue(number).get("_comments", []), query)

    def add_comment(self, query, body, number):
        self._issue(number).setdefault("_comments", []).append({"body": body["body"], "user": {"login": self.bot}})
        return {}

    def create_label(self, query, body):
        if body["name"] in self.labels:
            raise RuntimeError("GitHub API POST /labels: 422 b'already exists'")
        self.labels.add(body["name"])
        return {}

    def get_branch(self, query, body, name):
        name = unquote(name)
        if name not in self.branches:
            raise RuntimeError(f"GitHub API GET /branches/{name}: 404 b'Branch not found'")
        return {"name": name, "commit": {"sha": self.branches[name]}, "protected": False}

    def list_pulls(self, query, body):
        assert query["state"] == ["all"]  # a replay must find a pull request that is closed by now
        owner, _, branch = query["head"][0].partition(":")
        return [p for p in self.pulls.values() if p["head"]["ref"] == branch and owner == self.slug.split("/")[0]]

    def create_pull(self, query, body):
        if not body.get("draft"):
            raise AssertionError("flows only open drafts")
        number = self.next_number
        self.next_number += 1
        self.issues[number] = {"number": number, "pull_request": {}, "labels": [], "state": "open", "_comments": []}
        self.pulls[number] = {"number": number, "html_url": f"https://example.com/pull/{number}", "draft": True,
                              "state": "open", "merged": False, "node_id": f"PR_{number}", "title": body["title"],
                              "body": body["body"], "user": {"login": self.bot}, "base": {"ref": body["base"]},
                              "head": {"ref": body["head"], "sha": self.branches[body["head"]],
                                       "repo": {"full_name": self.slug}}, "_reviews": [], "_comments": []}
        return self.pulls[number]

    def get_pull(self, query, body, number):
        pull = self._pull(number)
        return {**pull, "head": {**pull["head"], "sha": self.branches.get(pull["head"]["ref"], pull["head"]["sha"])}}

    def patch_pull(self, query, body, number):
        self._pull(number).update(body)
        return {}

    def reviews(self, query, body, number):
        return self._page(self._pull(number)["_reviews"], query)

    def review_comments(self, query, body, number):
        return self._page(self._pull(number)["_comments"], query)

    def graphql(self, body):
        if self.fail_graphql:
            return {"data": None, "errors": [{"message": self.fail_graphql}]}
        for pull in self.pulls.values():
            if pull["node_id"] == body["variables"]["id"]:
                pull["draft"] = False
        return {"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}}

    ROUTES = (
        (r"GET /issues", list_issues),
        (r"GET /issues/(\d+)", get_issue),
        (r"GET /issues/(\d+)/events", events),
        (r"POST /issues/(\d+)/labels", add_labels),
        (r"DELETE /issues/(\d+)/labels/(.+)", remove_label),
        (r"GET /issues/(\d+)/comments", list_comments),
        (r"POST /issues/(\d+)/comments", add_comment),
        (r"POST /labels", create_label),
        (r"GET /branches/(.+)", get_branch),
        (r"GET /pulls", list_pulls),
        (r"POST /pulls", create_pull),
        (r"GET /pulls/(\d+)", get_pull),
        (r"PATCH /pulls/(\d+)", patch_pull),
        (r"GET /pulls/(\d+)/reviews", reviews),
        (r"GET /pulls/(\d+)/comments", review_comments),
    )

    # what people do on the other side

    def add_issue(self, key, title="A task", body="Do it", author="owner", labels=("flow:quick",),
                  label_actor="owner"):
        number = int(key)
        self.next_number = max(self.next_number, number + 1)
        self.issues[number] = {
            "number": number, "title": title, "body": body, "user": {"login": author}, "state": "open",
            "labels": [{"name": n} for n in labels], "closed_by": None, "_comments": [],
            "_events": [{"event": "labeled", "label": {"name": n}, "actor": {"login": label_actor}}
                        for n in labels if label_actor]}

    def close_issue(self, key, actor):
        self.issues[int(key)].update(state="closed", closed_by={"login": actor})

    def push(self, branch):
        sha = f"{len(self.branches) + 1:040x}"
        self.branches[branch] = sha
        return sha

    def add_review(self, pr, actor, state, commit, text=""):
        self.pulls[pr.number]["_reviews"].append({"user": {"login": actor}, "state": state.upper(), "commit_id": commit,
                                                  "body": text})

    def add_foreign_pull(self, branch, author="stranger", base="main", head_repo=None):
        number = self.next_number
        self.next_number += 1
        self.issues[number] = {"number": number, "pull_request": {}, "labels": [], "state": "open", "_comments": []}
        self.pulls[number] = {"number": number, "html_url": f"https://example.com/foreign/{number}", "draft": False,
                              "state": "open", "merged": False, "node_id": f"PR_{number}", "title": "Foreign", "body": "",
                              "user": {"login": author}, "base": {"ref": base},
                              "head": {"ref": branch, "sha": self.branches[branch],
                                       "repo": {"full_name": head_repo or self.slug}}, "_reviews": [], "_comments": []}
        return number

    def add_foreign_comment(self, number, body):
        self.issues[int(number)]["_comments"].append({"body": body, "user": {"login": "stranger"}})

    def add_note(self, pr, actor, text, commit):
        self.pulls[pr.number]["_comments"].append({"user": {"login": actor}, "body": text, "commit_id": "moved-on",
                                                   "original_commit_id": commit})

    def pr_text(self, pr):
        pull = self.pulls[pr.number]
        return pull["title"], pull["body"]

    def merge(self, pr):
        self.pulls[pr.number].update(state="closed", merged=True)

    def comments_of(self, pr):
        return [c["body"] for c in self.issues[pr.number]["_comments"]]

    def task_comments(self, key):
        return [c["body"] for c in self.issues[int(key)]["_comments"]]

    def task_state(self, key):
        states = [x["name"].removeprefix("anton:") for x in self.issues[int(key)]["labels"]
                  if x["name"].startswith("anton:")]
        return states[0] if len(states) == 1 else json.dumps(states)

"""Tests for the GraphQL validator module."""

import json

from bubble.graphql_validator import (
    ALLOWED_MUTATIONS,
    _count_operations,
    _extract_fields,
    _find_operation,
    _tokenize,
    extract_repo_from_preflight,
    parse_graphql,
    validate_read,
    validate_structure,
    validate_write,
)

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class TestTokenizer:
    def test_simple_query(self):
        tokens = _tokenize("query { repository { name } }")
        idents = [t.value for t in tokens if t.kind == "ident"]
        assert "query" in idents
        assert "repository" in idents
        assert "name" in idents

    def test_comments_stripped(self):
        tokens = _tokenize("# comment\nquery { repo { name } }")
        values = [t.value for t in tokens]
        assert "comment" not in " ".join(values)

    def test_strings_preserved(self):
        tokens = _tokenize('query { repo(name: "hello world") { id } }')
        strings = [t for t in tokens if t.kind == "string"]
        assert len(strings) == 1
        assert strings[0].value == '"hello world"'

    def test_spread(self):
        tokens = _tokenize("{ ... on PullRequest { id } }")
        spreads = [t for t in tokens if t.kind == "spread"]
        assert len(spreads) == 1

    def test_directive_detected(self):
        tokens = _tokenize("query @cached { repo { name } }")
        puncts = [t.value for t in tokens if t.kind == "punct"]
        assert "@" in puncts

    def test_block_string(self):
        tokens = _tokenize('mutation { addComment(body: """multi\nline""") { id } }')
        strings = [t for t in tokens if t.kind == "string"]
        assert len(strings) == 1

    def test_braces_in_string_dont_confuse_parser(self):
        """Braces inside strings should not affect brace counting."""
        tokens = _tokenize('query { repo(filter: "{ test }") { name } }')
        idents = [t.value for t in tokens if t.kind == "ident"]
        assert "repo" in idents
        assert "name" in idents


# ---------------------------------------------------------------------------
# Operation counting and finding
# ---------------------------------------------------------------------------


class TestOperationParsing:
    def test_single_query(self):
        tokens = _tokenize("query { repo { name } }")
        assert _count_operations(tokens) == 1

    def test_single_mutation(self):
        tokens = _tokenize("mutation { addComment(input: {}) { id } }")
        assert _count_operations(tokens) == 1

    def test_anonymous_query(self):
        tokens = _tokenize("{ repo { name } }")
        assert _count_operations(tokens) == 1

    def test_multiple_operations(self):
        tokens = _tokenize("query A { repo { name } } mutation B { addComment { id } }")
        assert _count_operations(tokens) == 2

    def test_fragment_not_counted(self):
        tokens = _tokenize("fragment F on Repo { name } query { repo { ...F } }")
        assert _count_operations(tokens) == 1

    def test_find_named_query(self):
        tokens = _tokenize("query MyQuery($x: String!) { repo { name } }")
        result = _find_operation(tokens)
        assert result is not None
        op_type, op_name, start, end = result
        assert op_type == "query"
        assert op_name == "MyQuery"

    def test_find_mutation(self):
        tokens = _tokenize(
            "mutation CreatePR($input: CreatePRInput!) { createPR(input: $input) { id } }"
        )
        result = _find_operation(tokens)
        assert result is not None
        assert result[0] == "mutation"
        assert result[1] == "CreatePR"

    def test_find_anonymous_query(self):
        tokens = _tokenize("{ repository { name } }")
        result = _find_operation(tokens)
        assert result is not None
        assert result[0] == "query"
        assert result[1] == ""

    def test_find_op_after_fragment(self):
        tokens = _tokenize("fragment F on Repo { name } query Q { repo { ...F } }")
        result = _find_operation(tokens)
        assert result is not None
        assert result[0] == "query"
        assert result[1] == "Q"


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------


class TestFieldExtraction:
    def test_simple_fields(self):
        tokens = _tokenize("{ name description url }")
        fields = _extract_fields(tokens, 0, len(tokens))
        names = [f.name for f in fields]
        assert names == ["name", "description", "url"]

    def test_field_with_args(self):
        tokens = _tokenize("{ pullRequest(number: 42) { title } }")
        fields = _extract_fields(tokens, 0, len(tokens))
        assert len(fields) == 1
        assert fields[0].name == "pullRequest"
        assert fields[0].has_args is True
        assert fields[0].selection_start is not None

    def test_alias_detected(self):
        tokens = _tokenize("{ myAlias: repository { name } }")
        fields = _extract_fields(tokens, 0, len(tokens))
        assert len(fields) == 1
        assert fields[0].name == "repository"
        assert fields[0].alias == "myAlias"

    def test_inline_fragment_skipped(self):
        tokens = _tokenize("{ ... on PullRequest { title } name }")
        fields = _extract_fields(tokens, 0, len(tokens))
        names = [f.name for f in fields]
        assert "name" in names

    def test_nested_selection_set(self):
        tokens = _tokenize("{ repository { pullRequest { title } } }")
        fields = _extract_fields(tokens, 0, len(tokens))
        assert len(fields) == 1
        assert fields[0].name == "repository"
        # Should have a selection set
        assert fields[0].selection_start is not None
        assert fields[0].selection_end is not None
        # Extract second level
        inner = _extract_fields(tokens, fields[0].selection_start, fields[0].selection_end)
        assert len(inner) == 1
        assert inner[0].name == "pullRequest"


# ---------------------------------------------------------------------------
# parse_graphql
# ---------------------------------------------------------------------------


class TestParseGraphql:
    def test_basic_query(self):
        q = (
            "query($owner: String!, $repo: String!)"
            " { repository(owner: $owner, name: $repo) { name } }"
        )
        body = json.dumps(
            {
                "query": q,
                "variables": {"owner": "my-org", "repo": "my-repo"},
            }
        ).encode()
        parsed, error = parse_graphql(body)
        assert error is None
        assert parsed.op_type == "query"
        assert len(parsed.top_level_fields) == 1
        assert parsed.top_level_fields[0].name == "repository"
        assert parsed.second_level_fields is not None
        assert parsed.second_level_fields[0].name == "name"
        assert parsed.variables["owner"] == "my-org"

    def test_mutation(self):
        body = json.dumps(
            {
                "query": (
                    "mutation CreatePR($input: CreatePullRequestInput!)"
                    " { createPullRequest(input: $input)"
                    " { pullRequest { id } } }"
                ),
                "variables": {"input": {"repositoryId": "abc123"}},
            }
        ).encode()
        parsed, error = parse_graphql(body)
        assert error is None
        assert parsed.op_type == "mutation"
        assert parsed.top_level_fields[0].name == "createPullRequest"

    def test_malformed_json(self):
        parsed, error = parse_graphql(b"not json")
        assert parsed is None
        assert "Malformed" in error

    def test_batched_rejected(self):
        body = json.dumps([{"query": "{ x }"}]).encode()
        parsed, error = parse_graphql(body)
        assert parsed is None
        assert "Batched" in error

    def test_subscription_rejected(self):
        body = json.dumps({"query": "subscription { onEvent { id } }"}).encode()
        parsed, error = parse_graphql(body)
        assert parsed is None
        assert "Subscription" in error

    def test_fragment_defs_detected(self):
        body = json.dumps(
            {
                "query": "fragment F on Repo { name } query { repository { ...F } }",
                "variables": {},
            }
        ).encode()
        parsed, error = parse_graphql(body)
        assert error is None
        assert parsed.has_fragment_defs is True

    def test_directives_detected(self):
        body = json.dumps(
            {
                "query": "query @cached { repository { name } }",
                "variables": {},
            }
        ).encode()
        parsed, error = parse_graphql(body)
        assert error is None
        assert parsed.has_directives is True


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------


class TestStructuralValidation:
    def _parse(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        parsed, error = parse_graphql(body)
        assert error is None, f"Parse error: {error}"
        tokens = _tokenize(query)
        op_count = _count_operations(tokens)
        return parsed, op_count

    def test_single_op_single_field_passes(self):
        parsed, op_count = self._parse(
            "query($o: String!, $r: String!) { repository(owner: $o, name: $r) { name } }"
        )
        assert validate_structure(parsed, op_count) is None

    def test_multiple_operations_rejected(self):
        parsed, op_count = self._parse(
            "query A { repository { name } } query B { repository { name } }"
        )
        error = validate_structure(parsed, op_count)
        assert error is not None
        assert "one operation" in error.lower()

    def test_multiple_top_level_fields_rejected(self):
        # Need to craft a query with multiple top-level non-scalar fields
        query = 'query { repository { name } node(id: "abc") { id } }'
        parsed, op_count = self._parse(query)
        error = validate_structure(parsed, op_count)
        assert error is not None
        assert "Multiple" in error

    def test_multiple_scalar_fields_allowed(self):
        """RepositoryInfo exception: multiple scalar fields under repository are OK,
        but multiple top-level fields are still rejected."""
        # Multiple scalar fields AT the top level (as if inside a repository selection)
        # would only happen if the top-level fields are all in ALLOWED_REPO_FIELDS
        # and have no selection set
        query = "query { name description url }"
        parsed, op_count = self._parse(query)
        # These are scalar fields in ALLOWED_REPO_FIELDS without selection sets
        error = validate_structure(parsed, op_count)
        assert error is None

    def test_alias_rejected(self):
        parsed, op_count = self._parse("query { myAlias: repository { name } }")
        error = validate_structure(parsed, op_count)
        assert error is not None
        assert "Alias" in error

    def test_directives_rejected(self):
        parsed, op_count = self._parse("query @cached { repository { name } }")
        error = validate_structure(parsed, op_count)
        assert error is not None
        assert "Directive" in error

    def test_fragments_in_mutations_rejected(self):
        query = (
            "fragment F on PullRequest { title } "
            "mutation { createPullRequest(input: $input) { pullRequest { ...F } } }"
        )
        parsed, op_count = self._parse(query)
        error = validate_structure(parsed, op_count)
        assert error is not None
        assert "Fragment" in error

    def test_fragments_in_queries_allowed(self):
        query = "fragment F on Repository { name } query { repository { ...F } }"
        parsed, op_count = self._parse(query)
        error = validate_structure(parsed, op_count)
        # Fragments in queries are fine
        assert error is None

    def test_mutation_no_fragments_passes(self):
        parsed, op_count = self._parse(
            "mutation { createPullRequest(input: $input) { pullRequest { id } } }"
        )
        assert validate_structure(parsed, op_count) is None


# ---------------------------------------------------------------------------
# Read validation
# ---------------------------------------------------------------------------


class TestReadValidation:
    def _parsed_query(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        parsed, error = parse_graphql(body)
        assert error is None
        return parsed

    def test_repository_query_allowed(self):
        q = (
            "query($owner: String!, $repo: String!)"
            " { repository(owner: $owner, name: $repo)"
            " { pullRequests { nodes { title } } } }"
        )
        parsed = self._parsed_query(q, {"owner": "my-org", "repo": "my-repo"})
        error = validate_read(parsed, "my-org", "my-repo")
        assert error is None

    _REPO_Q = (
        "query($owner: String!, $repo: String!) { repository(owner: $owner, name: $repo) { name } }"
    )

    def test_repository_wrong_owner_rejected(self):
        parsed = self._parsed_query(self._REPO_Q, {"owner": "hacker", "repo": "my-repo"})
        error = validate_read(parsed, "my-org", "my-repo")
        assert error is not None
        assert "mismatch" in error.lower()

    def test_repository_wrong_repo_rejected(self):
        parsed = self._parsed_query(self._REPO_Q, {"owner": "my-org", "repo": "other-repo"})
        error = validate_read(parsed, "my-org", "my-repo")
        assert error is not None
        assert "mismatch" in error.lower()

    def test_repository_case_insensitive(self):
        parsed = self._parsed_query(self._REPO_Q, {"owner": "My-Org", "repo": "My-Repo"})
        error = validate_read(parsed, "my-org", "my-repo")
        assert error is None

    def test_repository_missing_variables_rejected(self):
        parsed = self._parsed_query(
            "query($owner: String!) { repository(owner: $owner, name: $name) { name } }",
            {"owner": "my-org"},
        )
        error = validate_read(parsed, "my-org", "my-repo")
        assert error is not None
        assert "Missing" in error

    def test_inline_args_rejected(self):
        """Inline string arguments must be rejected to prevent bypass."""
        parsed = self._parsed_query(
            'query { repository(owner: "evil-org", name: "secret") { name } }',
            {"owner": "my-org", "repo": "my-repo"},
        )
        error = validate_read(parsed, "my-org", "my-repo")
        assert error is not None
        assert "$variable" in error

    def test_disallowed_second_level_field_rejected(self):
        q = (
            "query($owner: String!, $repo: String!)"
            " { repository(owner: $owner, name: $repo)"
            " { secretStuff { data } } }"
        )
        parsed = self._parsed_query(q, {"owner": "my-org", "repo": "my-repo"})
        error = validate_read(parsed, "my-org", "my-repo")
        assert error is not None
        assert "allowlist" in error.lower()

    def test_allowed_second_level_fields(self):
        """All the standard gh CLI second-level fields should be allowed."""
        for field_name in [
            "pullRequests",
            "pullRequest",
            "issues",
            "issue",
            "issueOrPullRequest",
            "labels",
            "releases",
        ]:
            q = (
                f"query($owner: String!, $repo: String!)"
                f" {{ repository(owner: $owner, name: $repo)"
                f" {{ {field_name} {{ nodes {{ id }} }} }} }}"
            )
            parsed = self._parsed_query(q, {"owner": "o", "repo": "r"})
            error = validate_read(parsed, "o", "r")
            assert error is None, f"Field {field_name} should be allowed but got: {error}"

    def test_type_introspection_allowed(self):
        parsed = self._parsed_query(
            'query { __type(name: "PullRequest") { fields { name } } }',
            {},
        )
        error = validate_read(parsed, "any-org", "any-repo")
        assert error is None

    def test_node_query_with_preflight(self):
        q = (
            "query($id: ID!) { node(id: $id)"
            " { ... on PullRequest"
            " { statusCheckRollup { state } } } }"
        )
        parsed = self._parsed_query(q, {"id": "PR_kwDOABC123"})
        # preflight_fn returns the allowed repo
        error = validate_read(
            parsed,
            "my-org",
            "my-repo",
            preflight_fn=lambda nid: "my-org/my-repo",
        )
        assert error is None

    def test_node_query_wrong_repo_rejected(self):
        parsed = self._parsed_query(
            "query($id: ID!) { node(id: $id) { ... on PullRequest { title } } }",
            {"id": "PR_kwDOABC123"},
        )
        error = validate_read(
            parsed,
            "my-org",
            "my-repo",
            preflight_fn=lambda nid: "hacker/evil-repo",
        )
        assert error is not None
        assert "belongs to" in error.lower()

    def test_node_query_missing_id_rejected(self):
        parsed = self._parsed_query(
            "query($id: ID!) { node(id: $id) { ... on PullRequest { title } } }",
            {},
        )
        error = validate_read(parsed, "o", "r")
        assert error is not None
        assert "Missing" in error

    def test_node_inline_id_rejected(self):
        """Inline node ID must be rejected to prevent bypass."""
        parsed = self._parsed_query(
            'query { node(id: "PR_foreign") { ... on PullRequest { title } } }',
            {"id": "PR_mine"},
        )
        error = validate_read(parsed, "o", "r")
        assert error is not None
        assert "$variable" in error

    def test_unknown_top_level_field_rejected(self):
        parsed = self._parsed_query(
            "query { viewer { login } }",
            {},
        )
        error = validate_read(parsed, "o", "r")
        assert error is not None
        assert "not allowed" in error.lower()

    def test_repo_variable_name_variant(self):
        """gh sometimes uses $name instead of $repo."""
        q = (
            "query($owner: String!, $name: String!)"
            " { repository(owner: $owner, name: $name) { name } }"
        )
        parsed = self._parsed_query(q, {"owner": "my-org", "name": "my-repo"})
        error = validate_read(parsed, "my-org", "my-repo")
        assert error is None


# ---------------------------------------------------------------------------
# Write validation
# ---------------------------------------------------------------------------


class TestWriteValidation:
    def _parsed_mutation(self, mutation_name, variables=None):
        query = f"mutation M($input: MInput!) {{ {mutation_name}(input: $input) {{ id }} }}"
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        parsed, error = parse_graphql(body)
        assert error is None
        return parsed

    def test_allowed_mutation_with_repo_id(self):
        parsed = self._parsed_mutation(
            "createPullRequest",
            {"input": {"repositoryId": "R_abc123", "title": "test"}},
        )
        error = validate_write(
            parsed,
            "my-org",
            "my-repo",
            repo_node_id_fn=lambda o, r: "R_abc123",
        )
        assert error is None

    def test_repo_id_mismatch_rejected(self):
        parsed = self._parsed_mutation(
            "createPullRequest",
            {"input": {"repositoryId": "R_wrong", "title": "test"}},
        )
        error = validate_write(
            parsed,
            "my-org",
            "my-repo",
            repo_node_id_fn=lambda o, r: "R_correct",
        )
        assert error is not None
        assert "mismatch" in error.lower()

    def test_allowed_mutation_with_preflight(self):
        parsed = self._parsed_mutation(
            "mergePullRequest",
            {"input": {"pullRequestId": "PR_abc123"}},
        )
        error = validate_write(
            parsed,
            "my-org",
            "my-repo",
            preflight_fn=lambda nid: "my-org/my-repo",
        )
        assert error is None

    def test_preflight_wrong_repo_rejected(self):
        parsed = self._parsed_mutation(
            "addComment",
            {"input": {"subjectId": "PR_abc123", "body": "test"}},
        )
        error = validate_write(
            parsed,
            "my-org",
            "my-repo",
            preflight_fn=lambda nid: "hacker/evil-repo",
        )
        assert error is not None
        assert "belongs to" in error.lower()

    def test_unknown_mutation_rejected(self):
        parsed = self._parsed_mutation(
            "deleteRepository",
            {"input": {"repositoryId": "R_abc123"}},
        )
        error = validate_write(parsed, "my-org", "my-repo")
        assert error is not None
        assert "not in allowlist" in error.lower()

    def test_missing_id_param_rejected(self):
        parsed = self._parsed_mutation(
            "mergePullRequest",
            {"input": {"someOtherId": "abc"}},
        )
        error = validate_write(parsed, "my-org", "my-repo")
        assert error is not None
        assert "Missing" in error

    def test_all_allowed_mutations_in_allowlist(self):
        """Verify the allowlist has the expected mutations."""
        expected_mutations = [
            "createPullRequest",
            "updatePullRequest",
            "closePullRequest",
            "reopenPullRequest",
            "mergePullRequest",
            "addComment",
            "createIssue",
            "closeIssue",
            "addLabelsToLabelable",
            "addReaction",
        ]
        for m in expected_mutations:
            assert m in ALLOWED_MUTATIONS, f"{m} should be in ALLOWED_MUTATIONS"

    def test_create_issue_repo_id_check(self):
        parsed = self._parsed_mutation(
            "createIssue",
            {"input": {"repositoryId": "R_abc123", "title": "bug"}},
        )
        error = validate_write(
            parsed,
            "org",
            "repo",
            repo_node_id_fn=lambda o, r: "R_abc123",
        )
        assert error is None

    def test_close_issue_preflight(self):
        parsed = self._parsed_mutation(
            "closeIssue",
            {"input": {"issueId": "I_abc123"}},
        )
        error = validate_write(
            parsed,
            "org",
            "repo",
            preflight_fn=lambda nid: "org/repo",
        )
        assert error is None

    def test_id_in_top_level_variables_fallback(self):
        """ID can be in top-level variables if not in input object."""
        query = "mutation M($input: AddCommentInput!) { addComment(input: $input) { id } }"
        body = json.dumps(
            {
                "query": query,
                "variables": {"input": {"subjectId": "PR_abc123", "body": "test"}},
            }
        ).encode()
        parsed, error = parse_graphql(body)
        assert error is None
        error = validate_write(
            parsed,
            "org",
            "repo",
            preflight_fn=lambda nid: "org/repo",
        )
        assert error is None

    def test_inline_input_object_rejected(self):
        """Inline input objects must be rejected to prevent bypass."""
        query = (
            "mutation M($id: ID!, $body: String!)"
            " { addComment(input: {subjectId: $id,"
            " body: $body}) { id } }"
        )
        body = json.dumps(
            {
                "query": query,
                "variables": {"id": "PR_abc123", "body": "test"},
            }
        ).encode()
        parsed, error = parse_graphql(body)
        assert error is None
        error = validate_write(parsed, "org", "repo")
        assert error is not None
        assert "$variable" in error


# ---------------------------------------------------------------------------
# extract_repo_from_preflight
# ---------------------------------------------------------------------------


class TestPreflightExtraction:
    def test_direct_repository(self):
        data = {"data": {"node": {"repository": {"nameWithOwner": "org/repo"}}}}
        assert extract_repo_from_preflight(data) == "org/repo"

    def test_via_pull_request(self):
        data = {"data": {"node": {"pullRequest": {"repository": {"nameWithOwner": "org/repo"}}}}}
        assert extract_repo_from_preflight(data) == "org/repo"

    def test_null_node(self):
        data = {"data": {"node": None}}
        assert extract_repo_from_preflight(data) is None

    def test_empty_response(self):
        assert extract_repo_from_preflight({}) is None
        assert extract_repo_from_preflight({"data": {}}) is None


# ---------------------------------------------------------------------------
# Backward compat: _resolve_graphql_policies
# ---------------------------------------------------------------------------


class TestResolveGraphqlPolicies:
    def test_explicit_policies_used(self):
        from bubble.auth_proxy import _resolve_graphql_policies

        info = {"level": 1, "graphql_read": "whitelisted", "graphql_write": "whitelisted"}
        assert _resolve_graphql_policies(info) == ("whitelisted", "whitelisted")

    def test_level_1_backward_compat(self):
        from bubble.auth_proxy import _resolve_graphql_policies

        info = {"level": 1}
        assert _resolve_graphql_policies(info) == ("none", "none")

    def test_level_3_backward_compat(self):
        from bubble.auth_proxy import _resolve_graphql_policies

        info = {"level": 3}
        assert _resolve_graphql_policies(info) == ("unrestricted", "none")

    def test_level_4_backward_compat(self):
        from bubble.auth_proxy import _resolve_graphql_policies

        info = {"level": 4}
        assert _resolve_graphql_policies(info) == ("unrestricted", "unrestricted")

    def test_invalid_policy_falls_back_to_level(self):
        from bubble.auth_proxy import _resolve_graphql_policies

        info = {"level": 3, "graphql_read": "invalid", "graphql_write": "typo"}
        # Invalid policies should fall back to level-based derivation
        assert _resolve_graphql_policies(info) == ("unrestricted", "none")


# ---------------------------------------------------------------------------
# Real-world gh CLI queries
# ---------------------------------------------------------------------------


class TestRealWorldQueries:
    """Test with queries that gh CLI actually generates."""

    def _validate_query(self, query, variables, owner, repo):
        body = json.dumps({"query": query, "variables": variables}).encode()
        parsed, error = parse_graphql(body)
        assert error is None, f"Parse error: {error}"
        tokens = _tokenize(parsed.query_str)
        op_count = _count_operations(tokens)
        error = validate_structure(parsed, op_count)
        assert error is None, f"Structure error: {error}"
        return validate_read(parsed, owner, repo)

    def test_pull_request_list(self):
        query = """query PullRequestList($owner: String!, $repo: String!, $limit: Int!) {
            repository(owner: $owner, name: $repo) {
                pullRequests(
                    first: $limit, states: OPEN,
                    orderBy: {field: CREATED_AT, direction: DESC}
                ) {
                    nodes {
                        number
                        title
                        headRefName
                    }
                }
            }
        }"""
        error = self._validate_query(query, {"owner": "o", "repo": "r", "limit": 30}, "o", "r")
        assert error is None

    def test_pull_request_by_number(self):
        query = """query PullRequestByNumber($owner: String!, $repo: String!, $number: Int!) {
            repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) {
                    title body state
                    headRefName baseRefName
                    mergeable
                }
            }
        }"""
        error = self._validate_query(query, {"owner": "o", "repo": "r", "number": 42}, "o", "r")
        assert error is None

    def test_issue_list(self):
        query = """query IssueList($owner: String!, $repo: String!) {
            repository(owner: $owner, name: $repo) {
                issues(first: 30, states: OPEN) {
                    nodes { number title }
                }
            }
        }"""
        error = self._validate_query(query, {"owner": "o", "repo": "r"}, "o", "r")
        assert error is None

    def test_repository_info(self):
        query = """query RepositoryInfo($owner: String!, $name: String!) {
            repository(owner: $owner, name: $name) {
                name
                owner { login }
                description
                url
                defaultBranchRef { name }
            }
        }"""
        error = self._validate_query(query, {"owner": "o", "name": "r"}, "o", "r")
        assert error is None

    def test_label_list(self):
        query = """query LabelList($owner: String!, $repo: String!) {
            repository(owner: $owner, name: $repo) {
                labels(first: 100) {
                    nodes { name color }
                }
            }
        }"""
        error = self._validate_query(query, {"owner": "o", "repo": "r"}, "o", "r")
        assert error is None

    def test_type_introspection(self):
        query = """query PullRequest_fields {
            __type(name: "PullRequest") {
                fields { name }
            }
        }"""
        error = self._validate_query(query, {}, "o", "r")
        assert error is None

    def test_status_checks(self):
        query = """query PullRequestStatusChecks($id: ID!) {
            node(id: $id) {
                ... on PullRequest {
                    statusCheckRollup {
                        state
                        contexts(first: 100) {
                            nodes {
                                ... on CheckRun { name conclusion }
                            }
                        }
                    }
                }
            }
        }"""
        body = json.dumps({"query": query, "variables": {"id": "PR_123"}}).encode()
        parsed, _ = parse_graphql(body)
        error = validate_read(parsed, "o", "r", preflight_fn=lambda nid: "o/r")
        assert error is None

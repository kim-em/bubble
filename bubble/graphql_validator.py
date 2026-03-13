"""GraphQL validation for the auth proxy.

Validates GraphQL queries and mutations against allowlists for
repo-scoped access control.  Designed for the simple query shapes
that the gh CLI generates.

Structural validation rejects complex GraphQL features (fragments
in mutations, aliases, directives, batched operations) that gh
doesn't use and that could bypass repo-scoping.

Semantic validation checks:
- Read queries against a field allowlist + repo-scoping
- Write mutations against a mutation allowlist + repo-scoping via
  pre-flight node ownership queries

GraphQL policies (per-token):
  graphql_read:  "whitelisted" | "unrestricted" | "none"
  graphql_write: "whitelisted" | "unrestricted" | "none"
"""

import json
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------------------
# Valid policy values
# ---------------------------------------------------------------------------

VALID_GRAPHQL_POLICIES = ("whitelisted", "unrestricted", "none")

# ---------------------------------------------------------------------------
# Mutation allowlist
# ---------------------------------------------------------------------------

# Each mutation maps to (id_param_name, verify_type):
# - id_param_name: key to look for in variables["input"]
# - verify_type: "repo_id" (compare with repo node ID) or
#                "preflight" (query node ownership)
ALLOWED_MUTATIONS: dict[str, tuple[str, str]] = {
    # PR operations
    "createPullRequest": ("repositoryId", "repo_id"),
    "updatePullRequest": ("pullRequestId", "preflight"),
    "closePullRequest": ("pullRequestId", "preflight"),
    "reopenPullRequest": ("pullRequestId", "preflight"),
    "mergePullRequest": ("pullRequestId", "preflight"),
    "markPullRequestReadyForReview": ("pullRequestId", "preflight"),
    "convertPullRequestToDraft": ("pullRequestId", "preflight"),
    "updatePullRequestBranch": ("pullRequestId", "preflight"),
    "requestReviews": ("pullRequestId", "preflight"),
    "enablePullRequestAutoMerge": ("pullRequestId", "preflight"),
    "disablePullRequestAutoMerge": ("pullRequestId", "preflight"),
    # Review operations
    "addPullRequestReview": ("pullRequestId", "preflight"),
    "addPullRequestReviewComment": ("pullRequestId", "preflight"),
    "submitPullRequestReview": ("pullRequestId", "preflight"),
    "resolveReviewThread": ("threadId", "preflight"),
    "unresolveReviewThread": ("threadId", "preflight"),
    "markFileAsViewed": ("pullRequestId", "preflight"),
    "unmarkFileAsViewed": ("pullRequestId", "preflight"),
    # Comment operations
    "addComment": ("subjectId", "preflight"),
    "updateIssueComment": ("id", "preflight"),
    # Issue operations
    "createIssue": ("repositoryId", "repo_id"),
    "closeIssue": ("issueId", "preflight"),
    "reopenIssue": ("issueId", "preflight"),
    "updateIssue": ("id", "preflight"),
    "pinIssue": ("issueId", "preflight"),
    "unpinIssue": ("issueId", "preflight"),
    # Metadata
    "addLabelsToLabelable": ("labelableId", "preflight"),
    "removeLabelsFromLabelable": ("labelableId", "preflight"),
    "addAssigneesToAssignable": ("assignableId", "preflight"),
    "removeAssigneesFromAssignable": ("assignableId", "preflight"),
    "addReaction": ("subjectId", "preflight"),
    "removeReaction": ("subjectId", "preflight"),
}

# ---------------------------------------------------------------------------
# Read query allowlists
# ---------------------------------------------------------------------------

# Second-level fields allowed under repository(owner, name) { ... }
ALLOWED_REPO_FIELDS: set[str] = {
    "pullRequests",
    "pullRequest",
    "issues",
    "issue",
    "issueOrPullRequest",
    "labels",
    "releases",
    # Scalar fields used by gh repo view (RepositoryInfo)
    "name",
    "owner",
    "description",
    "url",
    "defaultBranchRef",
    "hasIssuesEnabled",
    "isArchived",
    "isFork",
    "isPrivate",
    "createdAt",
    "updatedAt",
    "homepageUrl",
    "stargazerCount",
    "forkCount",
    "id",
    "viewerPermission",
    "visibility",
    "repositoryTopics",
    "primaryLanguage",
    "licenseInfo",
    "watchers",
    "fundingLinks",
    "codeOfConduct",
    "contactLinks",
    "parent",
    "mergeCommitAllowed",
    "rebaseMergeAllowed",
    "squashMergeAllowed",
    "deleteBranchOnMerge",
    "mirrorUrl",
    "autoMergeAllowed",
    "projects",
    "projectsV2",
}

# Top-level query fields that are allowed in whitelisted read mode
ALLOWED_TOP_LEVEL_QUERY_FIELDS: set[str] = {
    "repository",
    "node",
    "__type",
}

# ---------------------------------------------------------------------------
# Pre-flight query templates
# ---------------------------------------------------------------------------

REPO_ID_QUERY = (
    "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id } }"
)

# Comprehensive pre-flight query covering all object types that allowed
# mutations reference.  Resolves the owning repository's nameWithOwner.
PREFLIGHT_QUERY = """\
query($id: ID!) {
  node(id: $id) {
    ... on PullRequest { repository { nameWithOwner } }
    ... on Issue { repository { nameWithOwner } }
    ... on IssueComment { repository { nameWithOwner } }
    ... on PullRequestReview { pullRequest { repository { nameWithOwner } } }
    ... on PullRequestReviewComment { pullRequest { repository { nameWithOwner } } }
    ... on PullRequestReviewThread { pullRequest { repository { nameWithOwner } } }
  }
}"""


def extract_repo_from_preflight(data: dict) -> str | None:
    """Extract owner/repo from pre-flight query response.

    Handles both direct repository fields and nested (via pullRequest) paths.
    Returns "owner/repo" or None.
    """
    node = data.get("data", {}).get("node")
    if not node:
        return None

    # Direct: { repository { nameWithOwner } }
    repo = node.get("repository")
    if isinstance(repo, dict) and "nameWithOwner" in repo:
        return repo["nameWithOwner"]

    # Via pullRequest: { pullRequest { repository { nameWithOwner } } }
    pr = node.get("pullRequest")
    if isinstance(pr, dict):
        repo = pr.get("repository")
        if isinstance(repo, dict) and "nameWithOwner" in repo:
            return repo["nameWithOwner"]

    return None


# ---------------------------------------------------------------------------
# Lightweight GraphQL tokenizer
# ---------------------------------------------------------------------------


@dataclass
class _Token:
    kind: str  # "ident", "punct", "spread", "string"
    value: str
    pos: int


def _tokenize(text: str) -> list[_Token]:
    """Tokenize GraphQL text, stripping comments.

    Yields tokens of kind: ident, punct, spread, string.
    Handles line comments (#), regular strings ("..."), and
    block strings (triple-quoted).
    """
    tokens: list[_Token] = []
    i = 0
    n = len(text)

    while i < n:
        c = text[i]
        # Whitespace
        if c in " \t\r\n,":
            i += 1
            continue
        # Comment
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        # Block string
        if text[i : i + 3] == '"""':
            end = text.find('"""', i + 3)
            if end == -1:
                end = n
            else:
                end += 3
            tokens.append(_Token("string", text[i:end], i))
            i = end
            continue
        # String
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                j += 1
            j = min(j + 1, n)
            tokens.append(_Token("string", text[i:j], i))
            i = j
            continue
        # Spread (...)
        if text[i : i + 3] == "...":
            tokens.append(_Token("spread", "...", i))
            i += 3
            continue
        # Identifier
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(_Token("ident", text[i:j], i))
            i = j
            continue
        # Dollar variable
        if c == "$":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(_Token("ident", text[i:j], i))
            i = j
            continue
        # Number
        if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i + 1
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            i = j
            continue
        # Punctuation
        tokens.append(_Token("punct", c, i))
        i += 1

    return tokens


# ---------------------------------------------------------------------------
# Parsed GraphQL structure
# ---------------------------------------------------------------------------


@dataclass
class FieldInfo:
    """A field extracted from a GraphQL selection set."""

    name: str
    alias: str | None = None
    has_args: bool = False
    args: dict[str, str] | None = None  # argname -> "$var" or literal value
    selection_start: int | None = None  # token index of opening {
    selection_end: int | None = None  # token index after closing }


@dataclass
class ParsedGraphQL:
    """Parsed structure of a GraphQL request."""

    op_type: str  # "query" or "mutation"
    op_name: str  # Operation name (may be empty)
    top_level_fields: list[FieldInfo]
    has_fragment_defs: bool
    has_directives: bool
    second_level_fields: list[FieldInfo] | None = None
    variables: dict = field(default_factory=dict)
    query_str: str = ""


# ---------------------------------------------------------------------------
# Parsing functions
# ---------------------------------------------------------------------------


def _skip_braced_tokens(tokens: list[_Token], start: int) -> int:
    """Skip a balanced { ... } block in token list starting at start.

    tokens[start] must be a '{' punct token.
    Returns the index after the closing '}', or -1 on error.
    """
    if start >= len(tokens) or tokens[start].value != "{":
        return -1
    depth = 1
    i = start + 1
    while i < len(tokens) and depth > 0:
        if tokens[i].kind == "punct":
            if tokens[i].value == "{":
                depth += 1
            elif tokens[i].value == "}":
                depth -= 1
        i += 1
    return i if depth == 0 else -1


def _skip_parens(tokens: list[_Token], start: int) -> int:
    """Skip a balanced ( ... ) block in token list."""
    if start >= len(tokens) or tokens[start].value != "(":
        return -1
    depth = 1
    i = start + 1
    while i < len(tokens) and depth > 0:
        if tokens[i].kind == "punct":
            if tokens[i].value == "(":
                depth += 1
            elif tokens[i].value == ")":
                depth -= 1
        i += 1
    return i if depth == 0 else -1


def _extract_args(tokens: list[_Token], paren_start: int) -> dict[str, str]:
    """Extract simple keyword arguments from a field's argument list.

    Parses tokens between ( and ) looking for name: value pairs.
    Returns dict of argname -> value (e.g. "$owner" for variable refs,
    or the literal string for inline values).

    Only handles the top level — nested object values are skipped.
    """
    args: dict[str, str] = {}
    i = paren_start + 1  # skip (
    n = len(tokens)

    while i < n:
        tok = tokens[i]
        if tok.kind == "punct" and tok.value == ")":
            break

        # Look for: argName : value
        if tok.kind == "ident":
            arg_name = tok.value
            i += 1
            # Expect ':'
            if i < n and tokens[i].kind == "punct" and tokens[i].value == ":":
                i += 1
                if i < n:
                    val_tok = tokens[i]
                    if val_tok.kind == "ident" and val_tok.value.startswith("$"):
                        args[arg_name] = val_tok.value
                    elif val_tok.kind == "string":
                        args[arg_name] = val_tok.value
                    elif val_tok.kind == "ident":
                        # enum value or other literal
                        args[arg_name] = val_tok.value
                    elif val_tok.kind == "punct" and val_tok.value == "{":
                        # Object literal — skip it
                        skip = _skip_braced_tokens(tokens, i)
                        if skip != -1:
                            args[arg_name] = "{...}"
                            i = skip
                            continue
                    i += 1
                    continue

        i += 1

    return args


def _extract_fields(tokens: list[_Token], start: int, end: int) -> list[FieldInfo]:
    """Extract top-level fields from a selection set token range.

    tokens[start] is the '{' and tokens[end-1] is the '}'.
    Returns list of FieldInfo.
    """
    fields: list[FieldInfo] = []
    i = start + 1  # skip opening {
    limit = end - 1  # before closing }

    while i < limit:
        tok = tokens[i]

        # Skip inline fragments: ... on Type { ... }
        if tok.kind == "spread":
            i += 1
            # Skip 'on' keyword and type name
            while i < limit and tokens[i].kind == "ident":
                i += 1
            # Skip any selection set
            if i < limit and tokens[i].value == "{":
                skip = _skip_braced_tokens(tokens, i)
                if skip != -1:
                    i = skip
            continue

        # Field: identifier, optionally with alias, args, selection set
        if tok.kind == "ident":
            name = tok.value
            alias = None
            i += 1

            # Check for alias: name : realName
            if i < limit and tokens[i].kind == "punct" and tokens[i].value == ":":
                alias = name
                i += 1  # skip :
                if i < limit and tokens[i].kind == "ident":
                    name = tokens[i].value
                    i += 1

            # Extract arguments
            has_args = False
            args = None
            if i < limit and tokens[i].kind == "punct" and tokens[i].value == "(":
                has_args = True
                args = _extract_args(tokens, i)
                skip = _skip_parens(tokens, i)
                if skip != -1:
                    i = skip

            # Check for selection set
            sel_start = None
            sel_end = None
            if i < limit and tokens[i].kind == "punct" and tokens[i].value == "{":
                sel_start = i
                skip = _skip_braced_tokens(tokens, i)
                if skip != -1:
                    sel_end = skip
                    i = skip

            fields.append(
                FieldInfo(
                    name=name,
                    alias=alias,
                    has_args=has_args,
                    args=args,
                    selection_start=sel_start,
                    selection_end=sel_end,
                )
            )
            continue

        # Skip @ directives (we detect them but don't parse deeply)
        if tok.kind == "punct" and tok.value == "@":
            i += 1
            # Skip directive name
            if i < limit and tokens[i].kind == "ident":
                i += 1
            # Skip arguments
            if i < limit and tokens[i].kind == "punct" and tokens[i].value == "(":
                skip = _skip_parens(tokens, i)
                if skip != -1:
                    i = skip
            continue

        # Skip anything else
        i += 1

    return fields


def _find_operation(
    tokens: list[_Token],
) -> tuple[str, str, int, int] | None:
    """Find the first operation in a token list.

    Returns (op_type, op_name, body_start_idx, body_end_idx).
    body_start_idx is the index of the '{' token.
    """
    i = 0
    n = len(tokens)

    while i < n:
        tok = tokens[i]

        # Skip fragment definitions
        if tok.kind == "ident" and tok.value == "fragment":
            # fragment Name on Type { ... }
            while i < n and tokens[i].value != "{":
                i += 1
            if i < n:
                skip = _skip_braced_tokens(tokens, i)
                if skip != -1:
                    i = skip
            continue

        # Anonymous query: starts with {
        if tok.kind == "punct" and tok.value == "{":
            end = _skip_braced_tokens(tokens, i)
            if end == -1:
                return None
            return ("query", "", i, end)

        # Named operation: query/mutation [Name] [(vars)] { ... }
        if tok.kind == "ident" and tok.value.lower() in ("query", "mutation"):
            op_type = tok.value.lower()
            i += 1
            op_name = ""

            # Operation name (optional)
            if i < n and tokens[i].kind == "ident" and tokens[i].value[0] != "$":
                op_name = tokens[i].value
                i += 1

            # Skip variable definitions
            if i < n and tokens[i].kind == "punct" and tokens[i].value == "(":
                skip = _skip_parens(tokens, i)
                if skip != -1:
                    i = skip

            # Skip directives (@name or @name(...))
            while i < n and tokens[i].kind == "punct" and tokens[i].value == "@":
                i += 1
                if i < n and tokens[i].kind == "ident":
                    i += 1
                if i < n and tokens[i].kind == "punct" and tokens[i].value == "(":
                    skip = _skip_parens(tokens, i)
                    if skip != -1:
                        i = skip

            # Find body
            if i < n and tokens[i].kind == "punct" and tokens[i].value == "{":
                end = _skip_braced_tokens(tokens, i)
                if end == -1:
                    return None
                return (op_type, op_name, i, end)

            return None

        i += 1

    return None


def _count_operations(tokens: list[_Token]) -> int:
    """Count the number of operations in a token list."""
    count = 0
    i = 0
    n = len(tokens)

    while i < n:
        tok = tokens[i]

        # Fragment definitions don't count
        if tok.kind == "ident" and tok.value == "fragment":
            while i < n and tokens[i].value != "{":
                i += 1
            if i < n:
                skip = _skip_braced_tokens(tokens, i)
                if skip != -1:
                    i = skip
            continue

        # Anonymous query
        if tok.kind == "punct" and tok.value == "{":
            count += 1
            skip = _skip_braced_tokens(tokens, i)
            if skip != -1:
                i = skip
            else:
                i += 1
            continue

        # Named operation
        if tok.kind == "ident" and tok.value.lower() in (
            "query",
            "mutation",
            "subscription",
        ):
            count += 1
            # Skip to body
            i += 1
            while i < n and tokens[i].value != "{":
                if tokens[i].value == "(":
                    skip = _skip_parens(tokens, i)
                    if skip != -1:
                        i = skip
                    else:
                        i += 1
                else:
                    i += 1
            if i < n:
                skip = _skip_braced_tokens(tokens, i)
                if skip != -1:
                    i = skip
                else:
                    i += 1
            continue

        i += 1

    return count


# ---------------------------------------------------------------------------
# Main parsing entry point
# ---------------------------------------------------------------------------


def parse_graphql(body: bytes) -> tuple[ParsedGraphQL | None, str | None]:
    """Parse a GraphQL request body into a structured form.

    Returns (parsed, error). parsed is None on error.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "Malformed JSON body"

    if isinstance(data, list):
        return None, "Batched requests not allowed"

    if not isinstance(data, dict):
        return None, "Invalid request format"

    query_str = data.get("query")
    if not query_str or not isinstance(query_str, str):
        return None, "Missing or invalid 'query' field"

    variables = data.get("variables") or {}
    if not isinstance(variables, dict):
        return None, "Invalid 'variables' field"

    tokens = _tokenize(query_str)

    # Check for subscriptions
    for tok in tokens:
        if tok.kind == "ident" and tok.value.lower() == "subscription":
            return None, "Subscriptions not supported"

    # Count operations
    op_count = _count_operations(tokens)
    if op_count == 0:
        return None, "No operations found"

    # Find the first operation
    op_info = _find_operation(tokens)
    if not op_info:
        return None, "Could not parse operation"

    op_type, op_name, body_start, body_end = op_info

    # Check for fragment definitions
    has_fragment_defs = any(tok.kind == "ident" and tok.value == "fragment" for tok in tokens)

    # Check for directives (@ in tokens, outside of strings)
    has_directives = any(tok.kind == "punct" and tok.value == "@" for tok in tokens)

    # Extract top-level fields
    top_level = _extract_fields(tokens, body_start, body_end)

    # For repository queries, extract second-level fields
    second_level = None
    if (
        op_type == "query"
        and len(top_level) >= 1
        and top_level[0].name == "repository"
        and top_level[0].selection_start is not None
        and top_level[0].selection_end is not None
    ):
        second_level = _extract_fields(
            tokens, top_level[0].selection_start, top_level[0].selection_end
        )

    parsed = ParsedGraphQL(
        op_type=op_type,
        op_name=op_name,
        top_level_fields=top_level,
        has_fragment_defs=has_fragment_defs,
        has_directives=has_directives,
        second_level_fields=second_level,
        variables=variables,
        query_str=query_str,
    )

    return parsed, None


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------


def validate_structure(parsed: ParsedGraphQL, op_count: int = 1) -> str | None:
    """Structural validation: reject complex GraphQL patterns.

    Only accepts the simple shape that gh CLI generates.
    Returns error message or None if valid.
    """
    # Exactly one operation
    if op_count != 1:
        return f"Expected exactly one operation, found {op_count}"

    # Exactly one top-level field
    if len(parsed.top_level_fields) == 0:
        return "No fields in operation"
    if len(parsed.top_level_fields) > 1:
        # Allow multiple scalar fields for RepositoryInfo queries
        if parsed.op_type == "query" and all(
            f.name in ALLOWED_REPO_FIELDS and f.selection_start is None
            for f in parsed.top_level_fields
        ):
            pass  # RepositoryInfo exception: multiple scalar fields
        else:
            return "Multiple top-level fields not allowed"

    # No aliases on top-level fields
    if any(f.alias is not None for f in parsed.top_level_fields):
        return "Aliases not allowed on top-level fields"

    # No directives
    if parsed.has_directives:
        return "Directives not allowed"

    # No fragment definitions in mutations
    if parsed.op_type == "mutation" and parsed.has_fragment_defs:
        return "Fragment definitions not allowed in mutations"

    return None


# ---------------------------------------------------------------------------
# Read validation (whitelisted mode)
# ---------------------------------------------------------------------------


def validate_read(
    parsed: ParsedGraphQL,
    owner: str,
    repo: str,
    preflight_fn: Callable[[str], str | None] | None = None,
) -> str | None:
    """Validate a read query against the allowlist.

    preflight_fn: callable(node_id) -> "owner/repo" or None
    Returns error message or None if valid.
    """
    if not parsed.top_level_fields:
        return "No fields in query"

    field_name = parsed.top_level_fields[0].name

    # Schema introspection: allow unconditionally
    if field_name == "__type":
        return None

    if field_name == "repository":
        return _validate_repository_read(parsed, owner, repo)

    if field_name == "node":
        return _validate_node_read(parsed, owner, repo, preflight_fn)

    if field_name not in ALLOWED_TOP_LEVEL_QUERY_FIELDS:
        return f"Top-level field '{field_name}' not allowed"

    return None


def _validate_repository_read(parsed: ParsedGraphQL, owner: str, repo: str) -> str | None:
    """Validate a repository(...) { ... } query."""
    repo_field = parsed.top_level_fields[0]

    # Require variable references in arguments, not inline literals.
    # Without this, a query could use repository(owner: "evil", name: "secret")
    # while supplying benign variables, bypassing repo-scoping.
    args = repo_field.args or {}
    owner_arg = args.get("owner", "")
    name_arg = args.get("name", "")

    if not owner_arg.startswith("$") or not name_arg.startswith("$"):
        return "repository() arguments must use $variable references, not inline literals"

    # Resolve the variable names to their values
    owner_var = owner_arg[1:]  # strip $
    name_var = name_arg[1:]

    v = parsed.variables
    v_owner = v.get(owner_var, "")
    v_name = v.get(name_var, "")

    if not v_owner or not v_name:
        return "Missing owner/name variables for repository query"

    if v_owner.lower() != owner.lower() or v_name.lower() != repo.lower():
        return f"Repository mismatch: {v_owner}/{v_name} != {owner}/{repo}"

    # Validate second-level fields
    if parsed.second_level_fields:
        for sf in parsed.second_level_fields:
            if sf.name not in ALLOWED_REPO_FIELDS:
                return f"Field '{sf.name}' not in repository query allowlist"

    return None


def _validate_node_read(
    parsed: ParsedGraphQL,
    owner: str,
    repo: str,
    preflight_fn: Callable[[str], str | None] | None,
) -> str | None:
    """Validate a node(id: ...) query."""
    # Require variable reference for the node ID argument
    node_field = parsed.top_level_fields[0]
    args = node_field.args or {}
    id_arg = args.get("id", "")
    if not id_arg.startswith("$"):
        return "node() id argument must use $variable reference"

    id_var = id_arg[1:]
    node_id = parsed.variables.get(id_var)
    if not node_id:
        return "Missing 'id' variable for node query"

    if preflight_fn:
        node_repo = preflight_fn(node_id)
        if not node_repo:
            return "Could not verify node ownership"
        if node_repo.lower() != f"{owner}/{repo}".lower():
            return f"Node belongs to {node_repo}, not {owner}/{repo}"

    return None


# ---------------------------------------------------------------------------
# Write validation (whitelisted mode)
# ---------------------------------------------------------------------------


def validate_write(
    parsed: ParsedGraphQL,
    owner: str,
    repo: str,
    preflight_fn: Callable[[str], str | None] | None = None,
    repo_node_id_fn: Callable[[str, str], str | None] | None = None,
) -> str | None:
    """Validate a mutation against the allowlist.

    preflight_fn: callable(node_id) -> "owner/repo" or None
    repo_node_id_fn: callable(owner, repo) -> node_id or None
    Returns error message or None if valid.
    """
    if not parsed.top_level_fields:
        return "No fields in mutation"

    mutation_name = parsed.top_level_fields[0].name

    if mutation_name not in ALLOWED_MUTATIONS:
        return f"Mutation '{mutation_name}' not in allowlist"

    # Require mutation arguments use $variable references, not inline literals.
    # Without this, a mutation could use inline {pullRequestId: "foreign"}
    # while supplying a benign ID in variables, bypassing repo-scoping.
    mutation_field = parsed.top_level_fields[0]
    args = mutation_field.args or {}
    input_arg = args.get("input", "")
    if input_arg and not input_arg.startswith("$"):
        return "Mutation arguments must use $variable references, not inline literals"

    id_param, verify_type = ALLOWED_MUTATIONS[mutation_name]

    # Extract ID from variables — gh wraps mutation args in $input
    input_vars = parsed.variables.get("input")
    if isinstance(input_vars, dict):
        object_id = input_vars.get(id_param)
    else:
        object_id = None

    # Also check top-level variables as fallback
    if not object_id:
        object_id = parsed.variables.get(id_param)

    if not object_id or not isinstance(object_id, str):
        return f"Missing '{id_param}' in mutation variables"

    if verify_type == "repo_id":
        if repo_node_id_fn:
            expected_id = repo_node_id_fn(owner, repo)
            if not expected_id:
                return "Could not resolve repository node ID"
            if object_id != expected_id:
                return "Repository ID mismatch"
        return None

    if verify_type == "preflight":
        if preflight_fn:
            node_repo = preflight_fn(object_id)
            if not node_repo:
                return "Could not verify node ownership"
            if node_repo.lower() != f"{owner}/{repo}".lower():
                return f"Node belongs to {node_repo}, not {owner}/{repo}"
        return None

    return f"Unknown verification type: {verify_type}"

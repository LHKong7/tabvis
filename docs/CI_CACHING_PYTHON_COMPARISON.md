# GitHub Actions vs GitLab CI: Caching Python / pip Dependencies

> Sources: Official documentation from `docs.github.com` and `docs.gitlab.com` only.

---

## 1. GitHub Actions (docs.github.com)

In the *Caching Dependencies* section of its official *Building and testing Python* tutorial, GitHub
enables pip caching directly through the `cache: 'pip'` input built into `actions/setup-python`.

**Minimal YAML example** (source:
[docs.github.com – Building and testing Python → Caching Dependencies](https://docs.github.com/en/actions/tutorials/build-and-test-code/python#caching-dependencies)):

```yaml
steps:
  - uses: actions/checkout@v6
  - uses: actions/setup-python@v5
    with:
      python-version: '3.12'
      cache: 'pip'
  - run: pip install -r requirements.txt
  - run: pip test
```

Internally, `setup-python` calls `actions/cache@v4`. Its key combines the package manager, Python
version, and lock-file hash. By default, it automatically searches the repository for
`requirements.txt` (pip), `Pipfile.lock` (pipenv), or `poetry.lock` (poetry) when calculating that
hash (same source as above).

For finer control, such as custom paths or multiple keys, use `actions/cache` directly:

```yaml
- uses: actions/cache@v4
  with:
    path: ~/.cache/pip
    key: ${{ runner.os }}-pip-${{ hashFiles('**/requirements.txt') }}
    restore-keys: |
      ${{ runner.os }}-pip-
      ${{ runner.os }}-
```

**When a cache key becomes invalid**

GitHub's
[Dependency caching reference](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)
specifies:

- A key is a string calculated from expressions. If any expression result used in the key changes,
  such as `${{ runner.os }}`, the Python version, or the contents matched by
  `hashFiles('**/requirements.txt')`, the result is a completely different key and the previous
  cache no longer matches.
- When there is no partial-key match—neither an exact `key` match nor a `restore-keys` prefix
  match—the action treats it as a cache miss and writes a new cache under the new key after the job
  completes successfully.
- Because the `cache version` changes with `path` and compression-tool metadata, an old cache is not
  reused across incompatible runner operating systems or compression versions.
- Access is limited by branch and tag scope: caches from the current branch and default branch are
  mutually visible, while child branches, sibling branches, and different tags cannot see one
  another. See
  [Restrictions for accessing a cache](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache).
- Old caches are also removed by storage limits and eviction policy: by default, 10 GB per
  repository, a 5 GB limit per cache, eviction after more than seven days without access, and a
  maximum retention of 30 days. See *Usage limits and eviction policy* on the same page.

---

## 2. GitLab CI (docs.gitlab.com)

In the *Python* section of its official *CI/CD caching examples*, GitLab uses the `cache` keyword and
redirects pip's cache into the project directory with `PIP_CACHE_DIR`, because GitLab caches can
contain only paths inside the project directory.

**Minimal YAML example** (source:
[docs.gitlab.com – CI/CD caching examples → Python](https://docs.gitlab.com/ci/caching/examples/#python)):

```yaml
default:
  image: python:latest
  cache:
    paths:
      - .cache/pip
  variables:
    PIP_CACHE_DIR: "$CI_PROJECT_DIR/.cache/pip"
  before_script:
    - python -V
    - pip install virtualenv
    - virtualenv venv
    - source venv/bin/activate

test:
  script:
    - python setup.py test
    - pip install ruff
    - ruff --format=gitlab .
```

To make the key genuinely depend on a lock file, so dependency changes invalidate it, use
`cache:key:files`. GitLab demonstrates this in both *Cache strategies* and *CI/CD caching examples*:
[Compute the cache key from the lock file](https://docs.gitlab.com/ci/caching/examples/#compute-the-cache-key-from-the-lock-file).

```yaml
default:
  cache:
    key:
      files:
        - requirements.txt
    paths:
      - .cache/pip
```

**When a cache key becomes invalid**

GitLab's [Caching in GitLab CI/CD](https://docs.gitlab.com/ci/caching/) documentation specifies:

- A cache hit requires an exact `cache:key` match, either the exact string or the same hash computed
  by `cache:key:files`. Otherwise it is a miss, and a new cache is written under the current key when
  the job finishes.
- With `cache:key:files`, the key is derived from the listed files' hashes. Any lock-file content
  change, such as adding or removing a dependency or changing a version, changes the key and
  naturally invalidates the old cache.
- GitLab automatically adds a protected-branch suffix (`-protected` / `-non_protected`) to the key.
  The same key string is therefore treated as two different cache entries on protected and
  non-protected branches. See [Cache key names](https://docs.gitlab.com/ci/caching/#cache-key-names).
  A change in branch-protection status can consequently make the “same key” stop matching.
- Protected and non-protected branches do not share caches by default; this can be changed in
  settings.
- After a manual cache clear, new caches use the same key with an index suffix, so the original key
  no longer matches.
- Runner storage policy also affects cache lifetime. Self-managed runners need shared NFS or the S3
  and lifecycle-rule approach described in
  [distributed runner caching](https://docs.gitlab.com/runner/configuration/autoscale/#distributed-runners-caching).
  GitLab.com instance runners use object storage whose lifecycle expires objects. See
  [Good caching practices](https://docs.gitlab.com/ci/caching/#good-caching-practices).

---

## 3. Key Differences

| Dimension | GitHub Actions | GitLab CI |
| --- | --- | --- |
| Minimal declaration | `cache: 'pip'` input on `actions/setup-python` | `cache:` keyword plus `paths:` |
| Default key inputs | Runner OS + Python version + lock-file hash, calculated automatically | Explicitly supplied by the user, often `$CI_COMMIT_REF_SLUG`, or based on a lock-file hash through `cache:key:files` |
| Path restrictions | Any runner path, including `~/.cache/pip` | Must be inside `$CI_PROJECT_DIR`; otherwise redirect with `PIP_CACHE_DIR` |
| Branch scope | Visible only to the current and default branches; child/sibling branches and tags cannot see one another | Protected and non-protected branches are separate by default; different branches normally have separate caches |
| Lookup/fallback | Exact `key` → `restore-keys` prefixes → same process on the default branch | `key` → ordered `fallback_keys` → global `CACHE_FALLBACK_KEY` |
| Invalidation conditions | Expression, OS, Python version, or lock-file changes; scope mismatch; eviction | Lock-file changes, protected-branch suffix changes, manual clearing, object-storage lifecycle expiration |

---

## Source Links

- GitHub: <https://docs.github.com/en/actions/tutorials/build-and-test-code/python#caching-dependencies>
- GitHub: <https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching>
- GitHub: <https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache>
- GitLab: <https://docs.gitlab.com/ci/caching/examples/#python>
- GitLab: <https://docs.gitlab.com/ci/caching/examples/#compute-the-cache-key-from-the-lock-file>
- GitLab: <https://docs.gitlab.com/ci/caching/>
- GitLab: <https://docs.gitlab.com/ci/caching/#cache-key-names>

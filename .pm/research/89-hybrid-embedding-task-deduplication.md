# 89. Hybrid Embedding Task Deduplication

Date: 2026-04-26

## Requirement

Implement hybrid task deduplication beyond `pg_trgm` fuzzy matching using Azure OpenAI embeddings, with credentials loaded from environment variables and no committed secrets.

## Research

- Codemigo knowledge search: hybrid retrieval is strongest when semantic similarity is combined with structured filters and lexical search. For Charu, `user_id` and pending status must remain hard filters, `pg_trgm` should remain the cheap first pass, and embeddings should be a second-stage semantic fallback.
- Codemigo use-case search: cosine similarity is the standard directional comparison for dense embeddings; task-specific thresholds should be configurable and covered by deterministic tests rather than assumed from generic benchmarks.
- Microsoft Learn: Azure OpenAI embeddings require an endpoint, API key, and embedding model deployment name. Embeddings are vectors of floats where distance/similarity correlates with semantic similarity, and the latest embedding models support up to 8,192 input tokens.
- OpenAI cookbook: the Python OpenAI SDK supports `AzureOpenAI`/`AsyncAzureOpenAI` with `azure_endpoint`, `api_key`, and `api_version` loaded from environment variables.

## Design Decisions

- Keep `pg_trgm` as the first dedupe pass. It is local, cheap, deterministic, and already covered by property tests.
- Add Azure embedding dedupe only for `save_task`, because the backlog item is duplicate creation prevention. Completion/update/delete continue using the existing fuzzy matching threshold.
- Store task embeddings on the `tasks` table as JSONB to avoid adding and operating a new PostgreSQL extension in this pass. Charu task lists are small per user, so filtered Python cosine scoring is sufficient and easier to test.
- Store `embedding_model` and `embedding_updated_at` next to the vector so model changes can be detected and stale embeddings can be refreshed.
- Make the feature opt-in with `TASK_EMBEDDING_DEDUP_ENABLED`; Azure credentials and deployment names are read from env. This protects cost/privacy defaults and prevents tests from calling a live API.
- If embedding generation fails, task capture must still succeed through the existing `pg_trgm` behavior.

## Environment

Required when enabling semantic dedupe:

```env
TASK_EMBEDDING_DEDUP_ENABLED=true
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_VERSION=2025-03-01-preview
AZURE_OPENAI_EMBEDDING_MODEL=text-embedding-3-large
```

Optional:

```env
TASK_EMBEDDING_SIMILARITY_THRESHOLD=0.88
TASK_EMBEDDING_BACKFILL_LIMIT=25
AZURE_OPENAI_EMBEDDING_DIMENSIONS=
```

## Gotchas

- In Azure OpenAI, the `model` argument is the deployment name. The default assumes the deployment is named `text-embedding-3-large`; override the env value if Azure uses a custom deployment name.
- Embedding spaces are model-specific. When the model/deployment changes, existing stored embeddings should be treated as stale and refreshed lazily.
- Avoid false merges by keeping the semantic threshold conservative and user/status scoped.
- Never fail task creation because the embedding provider is unavailable.

## Required Packages

- `openai` as a direct dependency, even though it is currently present transitively through voice dependencies.

## Relevant Links

- https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/embeddings
- https://developers.openai.com/cookbook/examples/azure/embeddings

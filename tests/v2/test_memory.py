"""Durable memory: what gets stored, what gets retrieved, and — just as important — what
does not get retrieved."""
from __future__ import annotations

from agent_v2.memory import MemoryStore, contains_secret, domain_of


def test_retrieval_returns_only_relevant_rows(memory_store: MemoryStore):
    memory_store.save("preference", "The user prefers nonstop flights over connections")
    memory_store.save("site", "Results only load when you scroll to the bottom",
                      domain="listings.example")
    memory_store.save("lesson", "Sorting the table by price needs two clicks, not one",
                      domain="shop.example")
    memory_store.save("user_fact", "The user lives in Boston")

    result = memory_store.retrieve("book a flight to Denver", domain="airline.example")
    texts = " ".join(m.text for m in result.memories)
    assert "nonstop flights" in texts
    assert "Sorting the table by price" not in texts  # irrelevant to flights


def test_domain_match_promotes_a_site_memory(memory_store: MemoryStore):
    memory_store.save("site", "The listing page paginates ten items at a time",
                      domain="listings.example")
    memory_store.save("site", "The listing page paginates ten items at a time",
                      domain="other.example")
    result = memory_store.retrieve("read the listing", domain="listings.example", top_k=1)
    assert result.memories[0].domain == "listings.example"


def test_retrieval_is_bounded_by_top_k_and_token_budget(memory_store: MemoryStore):
    # Distinct domains, so nothing is superseded — 30 genuinely different rows that all
    # match the query, of which only a handful may reach the prompt.
    for i in range(30):
        memory_store.save("site", "The listing table needs sorting before the price is readable",
                          domain=f"shop{i}.example")
    result = memory_store.retrieve("sort the listing table by price", top_k=5, token_budget=400)
    assert len(result.memories) <= 5
    assert result.considered > 5  # it looked at many, it injected few


def test_a_changed_preference_supersedes_the_old_one(memory_store: MemoryStore):
    old = memory_store.save("preference", "The user prefers window seats when flying")
    new = memory_store.save("preference", "The user prefers aisle seats when flying")
    active = {m.id for m in memory_store.all_active()}
    assert new in active and old not in active


def test_unrelated_preferences_coexist(memory_store: MemoryStore):
    memory_store.save("preference", "The user prefers window seats when flying")
    memory_store.save("preference", "The user prefers metric units in summaries")
    assert len(memory_store.all_active()) == 2


def test_contradictory_site_knowledge_supersedes_rather_than_accumulating(memory_store: MemoryStore):
    memory_store.save("site", "The search box is in the top navigation bar", domain="a.example")
    memory_store.save("site", "The search box is in the top navigation bar on every page",
                      domain="a.example")
    rows = [m for m in memory_store.all_active() if m.domain == "a.example"]
    assert len(rows) == 1


def test_secrets_are_never_stored(memory_store: MemoryStore):
    assert memory_store.save("user_fact", "The user's password is: hunter2seventeen") is None
    assert memory_store.save("user_fact", "The 2FA code: 918273 worked") is None
    assert memory_store.save("site", "api_key = sk-abcdefghijklmnop") is None
    assert memory_store.all_active() == []


def test_secret_detector_lets_ordinary_text_through():
    assert not contains_secret("The site asks for a password before showing results")
    assert contains_secret("my password is swordfish")


def test_a_procedure_is_only_offered_once_it_has_worked(memory_store: MemoryStore):
    memory_store.save_procedure("find the cheapest listing",
                                ["open the listing page", "sort by price", "read the first row"],
                                domain="shop.example")
    assert memory_store.best_procedure("find the cheapest listing", "shop.example") is not None


def test_a_repeatedly_failing_procedure_stops_being_offered(memory_store: MemoryStore):
    pid = memory_store.save_procedure("find the cheapest listing", ["a", "b"], domain="shop.example")
    for _ in range(3):
        memory_store.record_procedure_outcome(pid, success=False)
    assert memory_store.best_procedure("find the cheapest listing", "shop.example") is None


def test_procedure_for_a_different_goal_is_not_offered(memory_store: MemoryStore):
    memory_store.save_procedure("find the cheapest listing", ["a", "b"], domain="shop.example")
    assert memory_store.best_procedure("write a summary of the news", "news.example") is None


def test_retrieval_across_tasks_is_the_point(memory_store: MemoryStore):
    memory_store.save("site", "Sorting requires clicking the column header twice",
                      domain="tables.example", source_task="task-one")
    result = memory_store.retrieve("sort the results", domain="tables.example")
    assert result.memories and result.memories[0].text.startswith("Sorting requires")


def test_use_counts_increase_when_a_memory_is_injected(memory_store: MemoryStore):
    memory_store.save("strategy", "Use the site's own search rather than guessing URLs")
    memory_store.retrieve("search the site")
    memory_store.retrieve("search the site")
    assert memory_store.all_active()[0].use_count == 2


def test_domain_normalization():
    assert domain_of("https://www.Example.com/a/b?c=1") == "example.com"
    assert domain_of("") == ""


def test_a_multi_domain_answer_is_reduced_to_one_host(memory_store: MemoryStore):
    """Asked which domain a memory belongs to, the model answers "python.org,nodejs.org" for
    a two-site task. Stored verbatim that never equals a real domain again, so the memory
    becomes unreachable by the domain signal."""
    memory_store.save("strategy", "Open each project's download page in its own tab",
                      domain="python.org,nodejs.org")
    stored = memory_store.all_active()[0]
    assert stored.domain == "python.org"

    result = memory_store.retrieve("compare downloads", domain="python.org")
    assert result.memories and result.memories[0].id == stored.id


def test_domain_normalization_handles_the_shapes_a_model_produces():
    from agent_v2.memory import normalize_domain
    assert normalize_domain("https://www.Example.com/a") == "example.com"
    assert normalize_domain("python.org, nodejs.org") == "python.org"
    assert normalize_domain("python.org and nodejs.org") == "python.org"
    assert normalize_domain("") == ""

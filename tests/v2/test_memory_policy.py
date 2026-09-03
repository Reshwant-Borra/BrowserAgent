"""What is allowed to outlive a task (V2 hardening §14/§15/§17).

The write filter is tested at the boundary — `MemoryStore.save` — rather than by grepping the
database afterwards. Grepping proves a particular string is absent; testing the boundary
proves the class of string cannot get in, which is what the credential guarantee needs.

Every rejected example below is either a row a real dev/holdout run actually produced (see
`evals/audit_memory.py`) or a direct instance of a category the spec names.
"""
from __future__ import annotations

import pytest

from agent_v2.memory import MemoryStore, WritePolicy, classify_write


# ---- sensitive material (V2 hardening §15) ------------------------------------------------

@pytest.mark.parametrize("text", [
    "The user's password is hunter2seventeen",
    "password: correct-horse-battery",
    "The passphrase is opensesame123",
    "The 2FA code 918273 worked for the login",
    "Their verification code is 40219",
    "918273 is the code for the account",
    "api_key = sk-abcdefghijklmnopqrstuvwx",
    "The bearer token is eyJhbGciOiJIUzI1NiJ9abcdef",
    "secret: s3cr3t-value-here",
    "The card number is stored in the profile",
    "Their SSN is on file with the provider",
])
def test_credentials_never_reach_the_store(memory_store: MemoryStore, text):
    assert memory_store.save("user_fact", text) is None
    assert classify_write(text)[0] == WritePolicy.SECRET
    assert memory_store.all_active() == []


def test_knowing_that_a_site_needs_a_password_is_still_storable(memory_store: MemoryStore):
    """The guarantee is about credentials, not about the word "password" — losing this would
    throw away genuinely useful navigation knowledge."""
    assert memory_store.save(
        "site", "This site asks for a password before it will show the archive",
        domain="a.example") is not None


# ---- transient values (V2 hardening §14) ----------------------------------------------------

@pytest.mark.parametrize("text", [
    "The vacuum cleaner costs $159.00 on the listing page",
    "The cheapest option was 89.99 USD",
    "There were 12 products in the comparison",
    "The search returned 7 results for that query",
    "As of 4 March 2026 the page listed three items",
    "Python version 3.14.7 is the latest",
    "Node.js stable version is v26.8.1",
    "The latest release is 3.13.1",
    "The best value candidate was the second row of the results table",
    "The page was updated 2026-03-04 with new entries",
])
def test_a_value_that_will_be_wrong_next_month_is_not_made_durable(memory_store: MemoryStore, text):
    assert memory_store.save("site", text, domain="a.example") is None
    assert classify_write(text)[0] == WritePolicy.TRANSIENT


@pytest.mark.parametrize("text", [
    "I successfully retrieved the latest stable version of Python from the official website",
    "I can compare version numbers between different software projects",
    "I need to compare major version numbers between the two download pages",
    "We clicked the Search button and the results appeared",
    "The goal was to quote a specific sentence from the domain's home page",
    "The task required extracting data but faced navigation issues",
    "The search returned nothing useful on this occasion",
])
def test_the_run_narrating_itself_is_not_made_durable(memory_store: MemoryStore, text):
    assert memory_store.save("user_fact", text) is None
    assert classify_write(text)[0] == WritePolicy.EPISODIC


def test_low_information_rows_are_refused(memory_store: MemoryStore):
    assert memory_store.save("site", "Learn more", domain="a.example") is None
    assert memory_store.save("site", "Currency Convert", domain="a.example") is None


def test_a_restatement_of_this_task_s_goal_and_numbers_is_refused(memory_store: MemoryStore):
    goal = "Find the price of the 1200 watt vacuum on shop.example"
    assert memory_store.save("strategy", "Find the price of the 1200 watt vacuum on shop.example",
                             goal=goal) is None


# ---- what must still get through ---------------------------------------------------------------

@pytest.mark.parametrize("type,text", [
    ("site", "Search results only load once you scroll to the bottom of the page"),
    ("site", "The downloads page lists the current version near the top of the main content"),
    ("strategy", "Open each project's page in its own tab rather than navigating back and forth"),
    ("lesson", "Clicking a disabled control changes nothing; look for the enabling checkbox first"),
    ("preference", "The user prefers nonstop flights and metric units in summaries"),
    ("site", "This shop shows prices only after you have chosen a delivery country"),
])
def test_genuinely_reusable_knowledge_is_still_stored(memory_store: MemoryStore, type, text):
    assert memory_store.save(type, text, domain="a.example") is not None, text
    assert classify_write(text, type=type)[0] == WritePolicy.ACCEPT


def test_rejections_are_counted_so_the_policy_can_be_measured(memory_store: MemoryStore):
    memory_store.save("site", "The item costs $99.00")
    memory_store.save("user_fact", "The user's password is hunter2seventeen")
    assert memory_store.rejections[WritePolicy.TRANSIENT] == 1
    assert memory_store.rejections[WritePolicy.SECRET] == 1
    assert "secret" in memory_store.last_rejection


# ---- cross-task isolation (V2 hardening §17) -----------------------------------------------------

def test_one_task_s_temporary_values_do_not_reach_the_next_task(memory_store: MemoryStore):
    """Task A shops for a vacuum and collects prices and a ranking. Task B is about train
    times. Nothing from A may surface in B — and nothing from A should have been durable in
    the first place."""
    for text in ["The Dyson V15 costs $649.99 on the listing page",
                 "The best value candidate was the second row of the results table",
                 "The search returned 24 vacuum cleaners"]:
        memory_store.save("site", text, domain="shop.example", source_task="task-a")
    assert memory_store.all_active() == []

    result = memory_store.retrieve("when is the next train to Cambridge", domain="rail.example")
    assert result.memories == []
    assert "649" not in result.render()


def test_but_a_genuinely_reusable_memory_from_an_earlier_task_still_arrives(
        memory_store: MemoryStore):
    """Selective persistence, not zero persistence: the durable lesson from task A is exactly
    what task C should be given."""
    memory_store.save("site", "Results on this site only load once you scroll to the bottom",
                      domain="shop.example", source_task="task-a")
    memory_store.save("preference", "The user prefers results sorted by price, cheapest first",
                      source_task="task-a")

    # Retrieval is deliberately task-agnostic: a later, unrelated task is exactly who this
    # was saved for.
    result = memory_store.retrieve("find a cheap kettle and sort the results",
                                   domain="shop.example")
    texts = " ".join(m.text for m in result.memories)
    assert "only load once you scroll" in texts
    assert "sorted by price" in texts


def test_decoys_do_not_crowd_out_the_memory_that_matters(memory_store: MemoryStore):
    """V2 hardening §16. Retrieval quality is measured the way it fails: a store full of
    plausible-but-irrelevant rows, and one row that actually answers the situation."""
    memory_store.save("site", "Search results only load once you scroll to the bottom",
                      domain="shop.example")
    for i in range(25):
        memory_store.save("site", f"Section {i} of this reference site is split across tabs",
                          domain=f"docs{i}.example")
        memory_store.save("lesson", f"Form {i} rejects an address without a postcode line",
                          domain=f"forms{i}.example")

    result = memory_store.retrieve("find a kettle in the shop listing", domain="shop.example",
                                   top_k=6, token_budget=380)
    texts = [m.text for m in result.memories]
    assert any("scroll to the bottom" in t for t in texts), texts
    assert len(result.memories) <= 6
    # …and the decoys that did come along are a minority of a small, bounded block
    assert sum(1 for t in texts if "reference site" in t or "postcode" in t) < len(texts)
    from agent.token_budget import count_tokens
    assert count_tokens(result.render()) <= 380


def test_a_stale_memory_from_an_unrelated_domain_is_outranked(memory_store: MemoryStore):
    memory_store.save("site", "The price filter is under the left sidebar", domain="shop.example")
    memory_store.save("site", "The price filter is under the left sidebar", domain="other.example")
    result = memory_store.retrieve("filter by price", domain="shop.example", top_k=1)
    assert result.memories[0].domain == "shop.example"


def test_retrieval_stays_bounded_even_with_a_large_store(memory_store: MemoryStore):
    for i in range(60):
        memory_store.save("site", f"Section {i} of this site needs its own tab to stay readable",
                          domain=f"s{i}.example")
    result = memory_store.retrieve("read a section of the site", top_k=6, token_budget=380)
    assert len(result.memories) <= 6
    assert result.considered > 6


# ---- BrowserAgent's own bookkeeping, mistaken for knowledge about the world ---------------

@pytest.mark.parametrize("text", [
    # The row that actually did the damage: written after a grounding challenge, retrieved
    # into a later task, and read there as "distrust what you can see".
    "Prices on this shop may include unverified data; verify critical values on the actual page.",
    "Some figures on the site are unsupported and need checking",
    "The listing data is not verified, so re-open each product page",
    "Evidence ids from the previous task can be reused here",
    "Grounding requires opening every source before answering",
])
def test_the_agents_own_verification_is_not_a_fact_about_the_world(memory_store: MemoryStore, text):
    assert classify_write(text)[0] == WritePolicy.SELF_REFERENTIAL
    assert memory_store.save("site", text) is None


@pytest.mark.parametrize("text", [
    "The product page shows a different price than the category listing",
    "This shop shows prices only after you have chosen a delivery country",
    "Search results only load once you scroll to the bottom",
])
def test_a_real_lesson_about_checking_a_page_still_survives(memory_store: MemoryStore, text):
    """The rule is about BrowserAgent's vocabulary, not about the idea of checking things —
    a genuine "look at the other page" lesson shares none of that vocabulary."""
    assert classify_write(text)[0] == WritePolicy.ACCEPT
    assert memory_store.save("site", text) is not None


# ---- the same narration, in the third person ----------------------------------------------

@pytest.mark.parametrize("text", [
    "User navigated to the catalogue and listed the titles it showed",
    "User attempted to find book prices on a catalogue site",
    "The user needed to compare book prices on a catalog page",
    "User clicked through to the product page to read the price",
])
def test_narration_is_refused_whoever_it_names(memory_store: MemoryStore, text):
    """The extractor writes "User navigated…" as readily as "I navigated…". Same sentence,
    same uselessness later."""
    assert classify_write(text)[0] == WritePolicy.EPISODIC
    assert memory_store.save("user_fact", text) is None


@pytest.mark.parametrize("text", [
    "The user prefers nonstop flights",
    "The user prefers window seats on long journeys",
    "The user wants prices shown including tax",
])
def test_a_real_user_preference_is_not_narration(memory_store: MemoryStore, text):
    assert classify_write(text)[0] == WritePolicy.ACCEPT
    assert memory_store.save("user_fact", text) is not None


# ---- which page of a listing something sat on ---------------------------------------------

@pytest.mark.parametrize("text", [
    "Book titles are displayed on the second page of the catalogue",
    "The remaining products appear on the third page of the listing",
    "The contact details are on the last page of the results",
])
def test_which_page_of_a_listing_something_was_on_is_transient(memory_store: MemoryStore, text):
    assert classify_write(text)[0] == WritePolicy.TRANSIENT
    assert memory_store.save("site", text) is None


def test_navigational_knowledge_about_paging_survives(memory_store: MemoryStore):
    """"Results continue on the next page" is how the site works; "the titles are on the
    second page" is what one listing looked like once."""
    text = "Results continue on the next page of the listing"
    assert classify_write(text)[0] == WritePolicy.ACCEPT
    assert memory_store.save("site", text) is not None


# ---- the policy applies on the way out as well as on the way in ---------------------------

def test_rows_the_current_policy_rejects_are_not_retrieved(memory_store: MemoryStore):
    """A store outlives the rules that filled it. A row admitted under an older policy is
    exactly as harmful as one admitted today, so retrieval re-checks rather than trusting
    that whatever is in the table was once allowed."""
    good = "Search results only load once you scroll to the bottom"
    bad = "Prices here may include unverified data; verify critical values on the actual page."
    memory_store.save("site", good, domain="shop.example")
    # Written straight past the write filter, standing in for a row from an older revision.
    memory_store.conn.execute(
        "INSERT INTO memories (type, text, domain, importance, created_at, updated_at)"
        " VALUES ('site', ?, 'shop.example', 0.9, '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00')", (bad,))
    memory_store.conn.commit()

    texts = [m.text for m in memory_store.retrieve("prices on the shop", domain="shop.example").memories]
    assert good in texts
    assert bad not in texts

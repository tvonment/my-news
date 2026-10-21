"""scripts/whatif_guard.py: the preprovision gate on `azd provision`.

Every rule gets a fixture that must block and one that must pass, because a
guard that blocks everything gets switched off and a guard that blocks nothing
is a green light painted on a wall. The fixtures are trimmed copies of what
`az deployment sub what-if --no-pretty-print -o json` prints: only the fields
a rule reads.

The guard lives outside backend/ (it runs under the operator's system python3
as azd's hook, not in the Functions app), so it is loaded from its path.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD_PATH = Path(__file__).resolve().parents[2] / "scripts" / "whatif_guard.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("whatif_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()

RG = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-mynews-mynews/providers"
COSMOS = f"{RG}/Microsoft.DocumentDB/databaseAccounts/cosmos-mynews-test"
DATABASE = f"{COSMOS}/sqlDatabases/mynews"
ITEMS = f"{DATABASE}/containers/items"
NEWS = f"{DATABASE}/containers/news"
FOUNDRY = f"{RG}/Microsoft.CognitiveServices/accounts/aif-mynews-test"
SITE = f"{RG}/Microsoft.Web/sites/func-mynews-test"
PLAN = f"{RG}/Microsoft.Web/serverfarms/plan-mynews-test"

PERIODIC = {
    "type": "Periodic",
    "periodicModeProperties": {
        "backupIntervalInMinutes": 1440,
        "backupRetentionIntervalInHours": 48,
        "backupStorageRedundancy": "Local",
    },
}
CONTINUOUS_7 = {"type": "Continuous", "continuousModeProperties": {"tier": "Continuous7Days"}}


# --- Fixture builders --------------------------------------------------------


def change(resource_id, change_type, before=None, after=None, delta=None, **extra):
    entry = {
        "resourceId": resource_id,
        "changeType": change_type,
        "unsupportedReason": None,
        "before": before,
        "after": after,
        "delta": delta,
    }
    entry.update(extra)
    return entry


def prop(path, kind, before=None, after=None, children=None):
    return {"path": path, "propertyChangeType": kind, "before": before, "after": after, "children": children}


def whatif(*changes, **extra):
    document = {"status": "Succeeded", "error": None, "changes": list(changes),
                "potentialChanges": None, "diagnostics": None}
    document.update(extra)
    return document


def account(free_tier=True, regions=(("Switzerland North", 0, False),),
            capabilities=("EnableNoSQLVectorSearch",), backup=PERIODIC):
    return {"properties": {
        "enableFreeTier": free_tier,
        "locations": [{"locationName": n, "failoverPriority": p, "isZoneRedundant": z} for n, p, z in regions],
        "capabilities": [{"name": c} for c in capabilities],
        "backupPolicy": backup,
    }}


def container(throughput=None, pk="/pk", vectors=None, autoscale=None):
    options = {}
    if throughput is not None:
        options["throughput"] = throughput
    if autoscale is not None:
        options["autoscaleSettings"] = {"maxThroughput": autoscale}
    resource = {"partitionKey": {"paths": [pk], "kind": "Hash", "version": 2}}
    if vectors is not None:
        resource["vectorEmbeddingPolicy"] = vectors
    return {"properties": {"options": options, "resource": resource}}


def site(always_ready=None):
    scale = {"instanceMemoryMB": 512, "maximumInstanceCount": 5}
    if always_ready is not None:
        scale["alwaysReady"] = always_ready
    return {"properties": {"functionAppConfig": {"scaleAndConcurrency": scale}}}


def judge(*changes, allow=(), **extra):
    return guard.evaluate(whatif(*changes, **extra), frozenset(allow))


FLOAT16 = {"vectorEmbeddings": [{"path": "/embedding", "dataType": "float16",
                                 "dimensions": 512, "distanceFunction": "cosine"}]}


# --- The baseline: a quiet provision passes ---------------------------------


def test_nochange_and_ignore_only_passes():
    report = judge(
        change(COSMOS, "NoChange", account(), account()),
        change(ITEMS, "NoChange", container(1000), container(1000)),
        change(PLAN, "NoChange", {"sku": {"name": "FC1"}}, {"sku": {"name": "FC1"}}),
        change(SITE, "NoChange", site(), site()),
        # The v1 model deployments after ai.bicep dropped them: Ignore, not Delete.
        change(f"{FOUNDRY}/deployments/scorer", "Ignore"),
    )
    assert report.blocked == [] and report.allowed == []
    assert report.exit_code == 0
    text = report.render()
    assert text.splitlines()[0] == "what-if: 5 resource(s): 4 NoChange, 1 Ignore"
    assert text.splitlines()[-1] == "what-if guard: pass"


def test_rule_ids_are_the_published_interface():
    # Operators type these into WHATIF_ALLOW and azure.yaml documents them;
    # renaming one silently turns an accepted change back into a block.
    assert guard.RULE_IDS == (
        "delete", "cosmos-free-tier", "cosmos-locations", "cosmos-capabilities-removed",
        "cosmos-backup", "cosmos-partition-key", "cosmos-vector-policy", "cosmos-throughput",
        "foundry-sku", "always-ready", "plan-sku",
    )


# --- One block and one pass per rule ----------------------------------------


class TestDelete:
    def test_any_delete_blocks(self):
        report = judge(change(f"{RG}/Microsoft.Storage/storageAccounts/stmynewstest", "Delete", {"sku": {}}))
        assert report.blocked == ["delete"]
        assert "Microsoft.Storage/storageAccounts/stmynewstest would be deleted" in report.findings["delete"]

    def test_ignore_is_not_a_delete(self):
        # Incremental mode: a resource the template stops naming is left alone.
        assert judge(change(f"{FOUNDRY}/deployments/summarizer", "Ignore")).blocked == []

    def test_a_property_delete_is_not_a_resource_delete(self):
        # Phase 0 removes app settings: Delete leaves inside a Modify, not a Delete.
        delta = [prop("properties.siteConfig.appSettings", "Array", children=[
            prop("6", "Delete", before={"name": "DIGEST_SCHEDULE", "value": "0 0 5,10,17 * * *"})])]
        assert judge(change(SITE, "Modify", site(), site(), delta)).blocked == []


class TestCosmosFreeTier:
    def test_turning_it_off_blocks(self):
        delta = [prop("properties.enableFreeTier", "Modify", True, False)]
        report = judge(change(COSMOS, "Modify", account(), account(free_tier=False), delta))
        assert report.blocked == ["cosmos-free-tier"]
        assert report.findings["cosmos-free-tier"] == [
            "Microsoft.DocumentDB/databaseAccounts/cosmos-mynews-test: enableFreeTier true -> false"]

    def test_a_new_account_blocks_and_names_the_likely_slip(self):
        report = judge(change(COSMOS, "Create", after=account()))
        assert report.blocked == ["cosmos-free-tier"]
        message = report.findings["cosmos-free-tier"][0]
        assert "a NEW Cosmos account" in message and "AZURE_ENV_NAME" in message

    def test_other_account_changes_pass(self):
        delta = [prop("properties.enableAutomaticFailover", "Create", None, True)]
        assert judge(change(COSMOS, "Modify", account(), account(), delta)).blocked == []


class TestCosmosLocations:
    def test_a_second_region_blocks(self):
        delta = [prop("properties.locations", "Array", children=[
            prop("1", "Create", after={"locationName": "westeurope", "failoverPriority": 1})])]
        after = account(regions=(("switzerlandnorth", 0, False), ("westeurope", 1, False)))
        report = judge(change(COSMOS, "Modify", account(), after, delta))
        assert report.blocked == ["cosmos-locations"]
        assert "switzerlandnorth -> switzerlandnorth, westeurope" in report.findings["cosmos-locations"][0]

    def test_a_nested_zone_flip_blocks_and_names_the_leaf(self):
        delta = [prop("properties.locations", "Array", children=[
            prop("0", "Modify", children=[prop("isZoneRedundant", "Modify", False, True)])])]
        after = account(regions=(("switzerlandnorth", 0, True),))
        report = judge(change(COSMOS, "Modify", account(), after, delta))
        assert report.blocked == ["cosmos-locations"]
        assert "(properties.locations[0].isZoneRedundant)" in report.findings["cosmos-locations"][0]

    def test_display_name_against_template_name_is_noise(self):
        # The API says "Switzerland North", the template "switzerlandnorth".
        delta = [prop("properties.locations", "Array", children=[
            prop("0", "Modify", children=[prop("locationName", "Modify", "Switzerland North", "switzerlandnorth")])])]
        after = account(regions=(("switzerlandnorth", 0, False),))
        assert judge(change(COSMOS, "Modify", account(), after, delta)).blocked == []


class TestCosmosCapabilities:
    def test_removing_one_blocks(self):
        delta = [prop("properties.capabilities", "Array", children=[
            prop("0", "Delete", before={"name": "EnableNoSQLVectorSearch"})])]
        report = judge(change(COSMOS, "Modify", account(), account(capabilities=()), delta))
        assert report.blocked == ["cosmos-capabilities-removed"]
        assert report.findings["cosmos-capabilities-removed"][0].endswith("removes EnableNoSQLVectorSearch")

    def test_adding_one_passes(self):
        delta = [prop("properties.capabilities", "Array", children=[
            prop("1", "Create", after={"name": "EnableNoSQLFullTextSearch"})])]
        after = account(capabilities=("EnableNoSQLVectorSearch", "EnableNoSQLFullTextSearch"))
        assert judge(change(COSMOS, "Modify", account(), after, delta)).blocked == []


class TestCosmosBackup:
    DELTA = [
        prop("properties.backupPolicy.type", "Modify", "Periodic", "Continuous"),
        prop("properties.backupPolicy.periodicModeProperties", "Delete", PERIODIC["periodicModeProperties"]),
        prop("properties.backupPolicy.continuousModeProperties", "Create", None, {"tier": "Continuous7Days"}),
    ]

    def test_the_phase_0_switch_blocks_until_allowed(self):
        args = (change(COSMOS, "Modify", account(), account(backup=CONTINUOUS_7), self.DELTA),)
        blocked = judge(*args)
        assert blocked.blocked == ["cosmos-backup"]
        assert blocked.findings["cosmos-backup"][0].endswith(
            "Periodic (every 1440 min, kept 48 h, Local) -> Continuous (Continuous7Days)")
        allowed = judge(*args, allow={"cosmos-backup"})
        assert allowed.blocked == [] and allowed.allowed == ["cosmos-backup"]
        assert allowed.exit_code == 0

    def test_read_only_noise_after_the_switch_passes(self):
        # Once Continuous, the API reports a migrationState the template never
        # sets; what-if marks it NoEffect and it must not trip the rule.
        before = account(backup=dict(CONTINUOUS_7, migrationState={"status": "Completed"}))
        delta = [prop("properties.backupPolicy.migrationState", "NoEffect", {"status": "Completed"})]
        assert judge(change(COSMOS, "Modify", before, account(backup=CONTINUOUS_7), delta)).blocked == []


class TestCosmosPartitionKey:
    def test_changing_it_blocks(self):
        delta = [prop("properties.resource.partitionKey.paths", "Array", children=[
            prop("0", "Modify", "/pk", "/id")])]
        report = judge(change(ITEMS, "Modify", container(1000), container(1000, pk="/id"), delta))
        assert report.blocked == ["cosmos-partition-key"]
        assert report.findings["cosmos-partition-key"][0].endswith(
            "partition key /pk (hash, v2) -> /id (hash, v2)")

    def test_a_new_container_with_its_own_key_passes(self):
        # Phase 1's `news`: a Create, the right way to get a different key.
        report = judge(change(ITEMS, "NoChange", container(400), container(400)),
                       change(NEWS, "Create", after=container(600, pk="/pk")))
        assert report.blocked == []


class TestCosmosVectorPolicy:
    def test_a_policy_appearing_on_an_existing_container_blocks(self):
        delta = [prop("properties.resource.vectorEmbeddingPolicy", "Create", None, FLOAT16)]
        report = judge(change(ITEMS, "Modify", container(1000), container(1000, vectors=FLOAT16), delta))
        assert report.blocked == ["cosmos-vector-policy"]
        assert report.findings["cosmos-vector-policy"][0].endswith("/embedding float16 x512")

    def test_a_new_container_with_a_policy_blocks(self):
        assert judge(change(NEWS, "Create", after=container(600, vectors=FLOAT16))).blocked == [
            "cosmos-vector-policy"]

    def test_an_empty_policy_passes(self):
        delta = [prop("properties.resource.vectorEmbeddingPolicy", "Create", None, {"vectorEmbeddings": []})]
        after = container(1000, vectors={"vectorEmbeddings": []})
        assert judge(change(ITEMS, "Modify", container(1000), after, delta)).blocked == []


class TestCosmosThroughput:
    def test_the_phase_1_split_fits(self):
        delta = [prop("properties.options.throughput", "Modify", 1000, 400)]
        report = judge(change(ITEMS, "Modify", container(1000), container(400), delta),
                       change(NEWS, "Create", after=container(600)))
        assert report.blocked == []

    def test_one_ru_over_blocks_and_counts_the_unchanged_container(self):
        report = judge(change(ITEMS, "NoChange", container(400), container(400)),
                       change(NEWS, "Create", after=container(601)))
        assert report.blocked == ["cosmos-throughput"]
        assert report.findings["cosmos-throughput"] == [
            "cosmos-mynews-test: 1001 RU/s in total (items 400, news 601), over the 1000 the free tier covers"]

    def test_shared_database_throughput_and_autoscale_count(self):
        report = judge(change(DATABASE, "Modify", {"properties": {"options": {}}},
                              {"properties": {"options": {"throughput": 400}}},
                              [prop("properties.options.throughput", "Create", None, 400)]),
                       change(ITEMS, "Modify", container(1000), container(autoscale=1000),
                              [prop("properties.options", "Modify")]))
        assert report.blocked == ["cosmos-throughput"]
        assert "mynews 400, items 1000 (autoscale max)" in report.findings["cosmos-throughput"][0]

    def test_throughput_settings_override_the_container_options(self):
        report = judge(change(ITEMS, "NoChange", container(400), container(400)),
                       change(f"{ITEMS}/throughputSettings/default", "Modify",
                              {"properties": {"resource": {"throughput": 400}}},
                              {"properties": {"resource": {"throughput": 1000}}},
                              [prop("properties.resource.throughput", "Modify", 400, 1000)]),
                       change(NEWS, "NoChange", container(600), container(600)))
        assert report.blocked == ["cosmos-throughput"]
        assert "items 1000, news 600" in report.findings["cosmos-throughput"][0]

    def test_a_deleted_container_no_longer_counts(self):
        # Phase 5: `items` goes, `news` rises to 1,000.
        report = judge(change(ITEMS, "Delete", container(400)),
                       change(NEWS, "Modify", container(600), container(1000),
                              [prop("properties.options.throughput", "Modify", 600, 1000)]),
                       allow={"delete"})
        assert "cosmos-throughput" not in report.findings
        assert report.blocked == [] and report.allowed == ["delete"]
        # Gone, not "throughput not visible": no warning for what is deleted.
        assert report.warnings == []

    def test_a_value_that_is_not_a_number_blocks(self):
        after = container("[parameters('containerThroughput')]")
        assert judge(change(ITEMS, "Modify", container(1000), after,
                            [prop("properties.options.throughput", "Modify")])).blocked == ["cosmos-throughput"]


class TestFoundrySku:
    @pytest.mark.parametrize("sku", ["GlobalStandard", "DataZoneStandard", "Standard"])
    def test_pay_per_token_skus_pass(self, sku):
        after = {"sku": {"name": sku, "capacity": 30}}
        assert judge(change(f"{FOUNDRY}/deployments/classifier", "Create", after=after)).blocked == []

    @pytest.mark.parametrize("sku", ["ProvisionedManaged", "DataZoneProvisionedManaged", None])
    def test_anything_else_blocks(self, sku):
        after = {"sku": {"name": sku, "capacity": 15}}
        report = judge(change(f"{FOUNDRY}/deployments/summarizer", "Create", after=after))
        assert report.blocked == ["foundry-sku"]


class TestAlwaysReady:
    def test_becoming_non_empty_blocks(self):
        ready = [{"name": "http", "instanceCount": 1}]
        delta = [prop("properties.functionAppConfig.scaleAndConcurrency.alwaysReady", "Create", None, ready)]
        report = judge(change(SITE, "Modify", site(), site(always_ready=ready), delta))
        assert report.blocked == ["always-ready"]
        assert "1 always-ready instance(s)" in report.findings["always-ready"][0]

    def test_a_new_app_with_one_blocks(self):
        report = judge(change(SITE, "Create", after=site(always_ready=[{"name": "http", "instanceCount": 1}])))
        assert report.blocked == ["always-ready"]

    @pytest.mark.parametrize("ready", [[], [{"name": "http", "instanceCount": 0}]])
    def test_empty_or_zero_passes(self, ready):
        delta = [prop("properties.functionAppConfig.scaleAndConcurrency.alwaysReady", "Create", None, ready)]
        assert judge(change(SITE, "Modify", site(), site(always_ready=ready), delta)).blocked == []

    def test_other_scale_changes_pass(self):
        # Phase 0 lowers the instance cap from 20 to 5.
        delta = [prop("properties.functionAppConfig.scaleAndConcurrency.maximumInstanceCount", "Modify", 20, 5)]
        assert judge(change(SITE, "Modify", site(), site(), delta)).blocked == []


class TestPlanSku:
    def test_leaving_flex_blocks(self):
        delta = [prop("sku.name", "Modify", "FC1", "EP1"), prop("sku.tier", "Modify", "FlexConsumption", "ElasticPremium")]
        report = judge(change(PLAN, "Modify", {"sku": {"name": "FC1"}}, {"sku": {"name": "EP1"}}, delta))
        assert report.blocked == ["plan-sku"]
        assert report.findings["plan-sku"][0].endswith("sku EP1, not FC1 (Flex Consumption)")

    def test_a_new_flex_plan_passes(self):
        assert judge(change(PLAN, "Create", after={"sku": {"name": "FC1", "tier": "FlexConsumption"}})).blocked == []


# --- WHATIF_ALLOW -------------------------------------------------------------


class TestAllow:
    def test_parsing_ignores_case_spaces_and_empties(self):
        assert guard.parse_allow(" Cosmos-Backup , ,delete,") == frozenset({"cosmos-backup", "delete"})
        assert guard.parse_allow(None) == frozenset()

    def test_allowing_one_rule_leaves_the_other_blocked(self):
        report = judge(
            change(f"{RG}/Microsoft.Storage/storageAccounts/old", "Delete", {}),
            change(PLAN, "Modify", {"sku": {"name": "FC1"}}, {"sku": {"name": "EP1"}},
                   [prop("sku.name", "Modify", "FC1", "EP1")]),
            allow={"delete"},
        )
        assert report.allowed == ["delete"] and report.blocked == ["plan-sku"]
        lines = report.render().splitlines()
        assert lines[-1].endswith("WHATIF_ALLOW=delete,plan-sku azd provision")
        assert any(line.startswith("allowed delete:") for line in lines)

    def test_unknown_and_unneeded_ids_are_reported_and_change_nothing(self):
        report = judge(change(f"{RG}/Microsoft.Storage/storageAccounts/old", "Delete", {}),
                       allow={"delte", "plan-sku"})
        assert report.blocked == ["delete"]
        text = report.render()
        assert "WHATIF_ALLOW names no rule called delte" in text
        assert "WHATIF_ALLOW lists plan-sku, but nothing here needed it" in text


# --- Reading the what-if ------------------------------------------------------


def test_nested_delta_children_flatten_to_full_paths():
    delta = [
        prop("properties.siteConfig.appSettings", "Array", children=[
            prop("6", "Delete", before={"name": "MODEL_SCORER", "value": "scorer"}),
            prop("7", "Modify", children=[prop("value", "Modify", "a", "b")]),
        ]),
        prop("properties.functionAppConfig.scaleAndConcurrency.maximumInstanceCount", "Modify", 20, 5),
        prop("properties.siteConfig.cors", "NoEffect"),
    ]
    assert [(leaf.path, leaf.kind) for leaf in guard.flatten(delta)] == [
        ("properties.siteConfig.appSettings[6]", "Delete"),
        ("properties.siteConfig.appSettings[7].value", "Modify"),
        ("properties.functionAppConfig.scaleAndConcurrency.maximumInstanceCount", "Modify"),
        ("properties.siteConfig.cors", "NoEffect"),
    ]
    line = next(line for line in judge(change(SITE, "Modify", site(), site(), delta)).render().splitlines()
                if "Microsoft.Web/sites" in line)
    # The setting's name is shown, its value never is; NoEffect is dropped.
    assert line == ("  ~ Modify Microsoft.Web/sites/func-mynews-test: -properties.siteConfig.appSettings[6] "
                    "(MODEL_SCORER), ~properties.siteConfig.appSettings[7].value, "
                    "~properties.functionAppConfig.scaleAndConcurrency.maximumInstanceCount")
    assert "scorer" not in line.replace("MODEL_SCORER", "")


def test_long_path_lists_are_cut_short():
    delta = [prop(f"properties.p{i}", "Modify", 1, 2) for i in range(12)]
    line = judge(change(SITE, "Modify", site(), site(), delta)).render().splitlines()[1]
    assert line.endswith(", +4 more")


def test_potential_changes_are_judged_and_labelled():
    report = judge(potentialChanges=[change(COSMOS, "Delete", account())])
    assert report.blocked == ["delete"]
    assert report.findings["delete"] == [
        "Microsoft.DocumentDB/databaseAccounts/cosmos-mynews-test (potential) would be deleted"]


def test_unsupported_resources_are_shown_and_their_rules_marked_unchecked():
    reason = "A nested deployment got short-circuited because a parameter could not be evaluated."
    report = judge(change(COSMOS, "NoChange", account(), account()),
                   change(SITE, "Unsupported", unsupportedReason=reason),
                   change(PLAN, "Unsupported", unsupportedReason=reason),
                   diagnostics=[{"level": "Warning", "code": "NestedDeploymentShortCircuited",
                                 "target": "functions", "message": "skipped"}])
    assert report.blocked == []
    assert report.not_checked() == ["always-ready", "plan-sku"]
    text = report.render()
    assert f"  x Unsupported Microsoft.Web/sites/func-mynews-test: {reason}" in text
    assert "  ! Warning NestedDeploymentShortCircuited at functions: skipped" in text
    assert "  not checked, their resources were Unsupported: always-ready, plan-sku" in text


def test_unknown_change_types_count_as_writes():
    report = judge(change(PLAN, "Replace", {"sku": {"name": "FC1"}}, {"sku": {"name": "EP1"}}))
    assert report.blocked == ["plan-sku"]


@pytest.mark.parametrize("document, message", [
    ([], "expected a JSON object"),
    ({"status": "Succeeded"}, "no 'changes'"),
    ({"changes": None}, "'changes' is none, not a list"),
    ({"changes": ["Modify"]}, "changes[0] is not an object"),
    ({"status": "Failed", "error": {"message": "InvalidTemplate"}, "changes": []}, "InvalidTemplate"),
])
def test_input_that_cannot_be_judged_raises(document, message):
    with pytest.raises(guard.InputError, match=message.replace("[", r"\[").replace("]", r"\]")):
        guard.evaluate(document)


# --- The command line ---------------------------------------------------------


def test_cli_reads_a_file_and_exits_1_on_a_block(tmp_path, capsys):
    path = tmp_path / "whatif.json"
    path.write_text(json.dumps(whatif(change(COSMOS, "Delete", account()))))
    assert guard.main([str(path)], environ={}) == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith("what-if guard: BLOCKED by delete.")
    assert guard.main([str(path)], environ={"WHATIF_ALLOW": "delete"}) == 0


def test_cli_reads_stdin(monkeypatch, capsys):
    data = json.dumps(whatif(change(COSMOS, "NoChange", account(), account()))).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xef\xbb\xbf" + data)))
    assert guard.main([], environ={}) == 0
    assert capsys.readouterr().out.strip().endswith("what-if guard: pass")


@pytest.mark.parametrize("content", ["", "not json", '{"changes": 3}'])
def test_cli_exits_2_on_input_it_cannot_judge(tmp_path, capsys, content):
    path = tmp_path / "whatif.json"
    path.write_text(content)
    assert guard.main([str(path)], environ={}) == 2
    assert capsys.readouterr().err.startswith("whatif_guard: ")


def test_cli_exits_2_on_a_missing_file(tmp_path):
    assert guard.main([str(tmp_path / "absent.json")], environ={}) == 2


def test_cli_help_and_usage(capsys):
    assert guard.main(["--help"], environ={}) == 0
    assert "WHATIF_ALLOW=cosmos-backup azd provision" in capsys.readouterr().out
    assert guard.main(["one.json", "two.json"], environ={}) == 2
    assert capsys.readouterr().err.startswith("usage:")


def test_runs_as_a_script(tmp_path):
    # The way whatif.sh calls it: a file argument, the verdict in the exit status.
    path = tmp_path / "whatif.json"
    path.write_text(json.dumps(whatif(change(PLAN, "Create", after={"sku": {"name": "Y1"}}))))
    result = subprocess.run([sys.executable, str(GUARD_PATH), str(path)], capture_output=True, text=True,
                            env={"WHATIF_ALLOW": ""})
    assert result.returncode == 1, result.stderr
    assert "BLOCKED plan-sku:" in result.stdout


def test_source_parses_as_python_3_9():
    # whatif.sh runs `python3`, which on macOS is the system 3.9. This catches
    # syntax from later versions (`match`, `except*`, type statements); CI
    # itself runs 3.12.
    ast.parse(GUARD_PATH.read_text(), feature_version=(3, 9))

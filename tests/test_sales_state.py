from stokozavr_bot.models import ClientProfile
from stokozavr_bot.sales_state import DEAL_STAGES, infer_deal_stage


def test_deal_stages_include_sales_funnel_not_sheet_statuses():
    assert "new_lead" in DEAL_STAGES
    assert "discovery" in DEAL_STAGES
    assert "qualified" in DEAL_STAGES
    assert "product_selection" in DEAL_STAGES
    assert "quote_requested" in DEAL_STAGES
    assert "objection" in DEAL_STAGES
    assert "ready_to_order" in DEAL_STAGES
    assert "новый" not in DEAL_STAGES
    assert "ожидает телефон" not in DEAL_STAGES


def test_empty_profile_is_new_lead():
    assert infer_deal_stage(ClientProfile(1)) == "new_lead"


def test_name_or_phone_without_product_is_discovery():
    assert infer_deal_stage(ClientProfile(1, name="Анна")) == "discovery"
    assert infer_deal_stage(ClientProfile(2, phone="+79991234567")) == "discovery"


def test_product_without_volume_is_product_selection():
    profile = ClientProfile(1, name="Анна", phone="+79991234567", product="масло")
    assert infer_deal_stage(profile) == "product_selection"


def test_product_and_volume_are_qualified():
    profile = ClientProfile(
        1, name="Анна", phone="+79991234567", product="масло", volume="10 коробок"
    )
    assert infer_deal_stage(profile) == "qualified"


def test_question_without_product_is_discovery_not_form_gate():
    assert infer_deal_stage(ClientProfile(1), intent="question") == "discovery"


def test_intent_overlays_quote_objection_and_order():
    profile = ClientProfile(1, name="Анна", product="масло", volume="2 тонны")
    assert infer_deal_stage(profile, intent="quote_requested") == "quote_requested"
    assert infer_deal_stage(profile, intent="objection") == "objection"
    assert infer_deal_stage(profile, intent="ready_to_order") == "ready_to_order"


def test_infer_deal_stage_does_not_mutate_sheets_status():
    profile = ClientProfile(1, name="Анна", status="ожидает телефон")
    assert infer_deal_stage(profile, intent="question") == "discovery"
    assert profile.status == "ожидает телефон"

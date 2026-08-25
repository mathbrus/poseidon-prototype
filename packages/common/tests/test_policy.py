from common.policy import company_decision_path, company_package, rego_slug


def test_rego_slug_makes_a_name_an_identifier():
    assert rego_slug("a.b-c/d") == "a_b_c_d"


def test_decision_path_is_the_package_path():
    assert company_package("hello-saas") == "poseidon.authz.hello_saas"
    assert company_decision_path("hello-saas") == "poseidon/authz/hello_saas/decision"

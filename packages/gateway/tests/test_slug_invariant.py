"""The gateway's decision path and the compiler's bundle package implement
the same company-slug transform independently. Divergence fails closed
(every decision for the company lands on an empty document and denies),
but silently — this is the test that makes the invariant loud.

The compiler import comes from the uv workspace (both packages install
into the same environment); this is deliberately a cross-package test.
"""

import pytest

from compiler.rego import company_package
from gateway.opa import company_decision_path


@pytest.mark.parametrize(
    "company",
    [
        "hello-saas",
        "hello_saas",
        "hello.saas",
        "org/team",
        "a-b.c/d_e",
        "UPPER-Case",
        "plain",
    ],
)
def test_gateway_path_and_compiler_package_agree(company):
    package_path = company_package(company).replace(".", "/")
    assert company_decision_path(company) == f"{package_path}/decision"

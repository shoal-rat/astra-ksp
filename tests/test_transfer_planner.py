import math

import pytest

from ksp_lab import transfer_planner as tp


def test_ejection_periapsis_uses_hyperbolic_asymptote_true_anomaly():
    vinf = (918.3458304101223, 0.0, 0.0)
    r_park = 700_000.0
    mu_kerbin = 3.5316e12
    h_hat = (0.0, 0.0, 1.0)

    r_peri_hat, nu_inf = tp.ejection_periapsis_direction(vinf, r_park, mu_kerbin, h_hat)
    e_hyp = 1.0 + r_park * tp.vnorm(vinf) ** 2 / mu_kerbin

    assert math.degrees(nu_inf) > 90.0
    assert tp.vdot(r_peri_hat, tp.vunit(vinf)) == pytest.approx(-1.0 / e_hyp, rel=1e-12)

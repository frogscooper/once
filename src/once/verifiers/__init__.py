"""Ready-made verify callbacks for common services.

    from once.verifiers import http_verifier, stripe_params, stripe_verifier

Nothing here imports a third party package until it is used.
"""

from ._http import http_verifier
from ._stripe import stripe_params, stripe_verifier

__all__ = ["http_verifier", "stripe_params", "stripe_verifier"]

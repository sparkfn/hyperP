"""SG Gov dump connectors."""

from src.connectors.sggov.bankruptcy import SGGovernmentBankruptcyConnector
from src.connectors.sggov.rental_flats import SGGovernmentRentalFlatsConnector

__all__ = ["SGGovernmentBankruptcyConnector", "SGGovernmentRentalFlatsConnector"]

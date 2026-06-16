from pathlib import Path
from datetime import datetime
from tricount import load_client, Category
from config import CREDENTIALS_PATH


def _parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


class TricountManager:
    def __init__(self):
        self.client = None
        self.tricount = None

    def get_client(self):
        if self.client is None:
            creds_path = Path(CREDENTIALS_PATH)
            self.client = load_client(str(creds_path))
        return self.client

    def join_tricount(self, sharing_token):
        client = self.get_client()
        self.tricount = client.join_tricount(sharing_token, fetch_full=True)
        return self.tricount

    def refresh_tricount(self):
        if self.tricount:
            client = self.get_client()
            self.tricount = client.join_tricount(
                self.tricount.public_identifier_token, fetch_full=True
            )
        return self.tricount

    @property
    def members(self):
        if self.tricount:
            return self.tricount.members
        return []

    @property
    def transactions(self):
        if self.tricount:
            return self.tricount.transactions
        return []

    def get_balances(self):
        if not self.tricount:
            return {}
        client = self.get_client()
        return client.get_balances(self.tricount)

    def create_transaction(self, description, amount, payer, split_among, category=None, date=None):
        client = self.get_client()
        kwargs = dict(
            tricount=self.tricount,
            description=description,
            amount=float(amount),
            payer=payer,
            split_among=split_among,
        )
        if category and category != "OTHER":
            kwargs["category"] = getattr(Category, category, Category.OTHER)
        elif category == "OTHER":
            kwargs["category"] = Category.OTHER
        parsed = _parse_date(date)
        if parsed:
            kwargs["date"] = parsed
        return client.create_transaction(**kwargs)

    def create_transaction_custom_split(self, description, amount, payer, allocations, category=None, date=None):
        client = self.get_client()
        kwargs = dict(
            tricount=self.tricount,
            description=description,
            amount=float(amount),
            payer=payer,
            allocations=allocations,
        )
        if category and category != "OTHER":
            kwargs["category"] = getattr(Category, category, Category.OTHER)
        parsed = _parse_date(date)
        if parsed:
            kwargs["date"] = parsed
        return client.create_transaction_custom_split(**kwargs)

    def create_reimbursement(self, payer, receiver, amount, description, date=None):
        client = self.get_client()
        kwargs = dict(
            tricount=self.tricount,
            payer=payer,
            receiver=receiver,
            amount=float(amount),
            description=description,
        )
        parsed = _parse_date(date)
        if parsed:
            kwargs["date"] = parsed
        return client.create_reimbursement(**kwargs)

    def edit_transaction(self, transaction_id, description, amount, category=None, date=None):
        client = self.get_client()
        kwargs = dict(
            tricount=self.tricount,
            transaction_id=transaction_id,
            description=description,
            amount=float(amount),
        )
        if category and category != "OTHER":
            kwargs["category"] = getattr(Category, category, Category.OTHER)
        parsed = _parse_date(date)
        if parsed:
            kwargs["date"] = parsed
        return client.edit_transaction(**kwargs)

    def delete_transaction(self, transaction_id):
        client = self.get_client()
        return client.delete_transaction(self.tricount, transaction_id=transaction_id)

    def add_members(self, names):
        client = self.get_client()
        return client.add_members(self.tricount, names)

    def rename_member(self, member, new_name):
        client = self.get_client()
        return client.rename_member(self.tricount, member, new_name)

    def delete_member(self, member):
        client = self.get_client()
        return client.delete_member(self.tricount, member)

    def get_member_by_name(self, name):
        if self.tricount:
            return self.tricount.get_member_by_name(name)
        return None

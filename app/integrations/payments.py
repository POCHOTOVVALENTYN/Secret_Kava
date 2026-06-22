# app/integrations/payments.py
import base64
import hashlib
import hmac
import json
from typing import Any
import httpx
from structlog import get_logger

logger = get_logger()

class MonobankPaymentClient:
    """Enterprise client integration for generating Monobank dynamic invoices and validating hooks."""
    
    def __init__(self, api_token: str, webhook_url: str):
        self.api_token = api_token
        self.webhook_url = webhook_url
        self.base_url = "https://api.monobank.ua"
        self.headers = {"X-Token": self.api_token}

    async def create_invoice(self, amount: float, order_id: str, client_name: str) -> tuple[str, str]:
        """Creates a Monobank Invoice returning a payment checkout page and invoice ID."""
        url = f"{self.base_url}/api/merchant/invoice/create"
        
        # Convert dynamic decimal amount to Monobank cents integers (UAH 100.50 -> 10050)
        amount_cents = int(amount * 100)
        
        payload = {
            "amount": amount_cents,
            "ccy": 980, # UAH currency code
            "merchantPaymInfo": {
                "reference": order_id,
                "destination": f"Оплата послуг психолога для {client_name}",
                "comment": f"Рахунок: {order_id}"
            },
            "redirectUrl": "https://t.me/psy_space_bot",
            "webHookUrl": self.webhook_url
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, headers=self.headers, json=payload)
            if response.status_code != 200:
                logger.error("monobank_invoice_failed", code=response.status_code, body=response.text)
                raise RuntimeError("Failed to create Monobank invoice")
                
            res_data = response.json()
            return res_data["pageUrl"], res_data["invoiceId"]

    def verify_webhook_signature(self, request_body: bytes, signature_header: str, public_key: str) -> bool:
        """Validates that incoming webhook payload matches official Monobank cryptographic signatures."""
        try:
            key_bytes = bytes.fromhex(public_key)
            calculated_signature = hmac.new(key_bytes, request_body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(calculated_signature, signature_header)
        except Exception as e:
            logger.error("monobank_signature_verification_exception", error=str(e))
            return False


class LiqPayPaymentClient:
    """Enterprise client integration for generating LiqPay dynamic checkout forms and webhooks."""

    def __init__(self, public_key: str, private_key: str, webhook_url: str):
        self.public_key = public_key
        self.private_key = private_key
        self.webhook_url = webhook_url
        self.base_url = "https://www.liqpay.ua/api/3/checkout"

    def _generate_signature(self, data: str) -> str:
        """Generates LiqPay SHA1 signature from data string using private key."""
        sign_str = self.private_key + data + self.private_key
        return base64.b64encode(hashlib.sha1(sign_str.encode("utf-8")).digest()).decode("utf-8")

    def create_checkout_params(self, amount: float, order_id: str, description: str) -> dict[str, str]:
        """Generates standard Base64 encoded payload and signature parameters for LiqPay checkout."""
        params = {
            "public_key": self.public_key,
            "version": "3",
            "action": "pay",
            "amount": amount,
            "currency": "UAH",
            "description": description,
            "order_id": order_id,
            "server_url": self.webhook_url,
            "result_url": "https://t.me/psy_space_bot",
        }
        
        encoded_data = base64.b64encode(json.dumps(params).encode("utf-8")).decode("utf-8")
        signature = self._generate_signature(encoded_data)
        
        return {
            "data": encoded_data,
            "signature": signature
        }

    def verify_webhook(self, data: str, signature: str) -> bool:
        """Verifies that LiqPay callback data payload signature matches signature header."""
        calculated = self._generate_signature(data)
        return hmac.compare_digest(calculated, signature)


class WayForPayPaymentClient:
    """Enterprise client integration for generating WayForPay dynamic invoices and validating hooks."""
    
    def __init__(self, merchant_account: str, secret_key: str, webhook_url: str, domain: str = "secretcava.com.ua"):
        self.merchant_account = merchant_account
        self.secret_key = secret_key
        self.webhook_url = webhook_url
        self.domain = domain
        self.base_url = "https://api.wayforpay.com/api"

    def _generate_signature(self, data_str: str) -> str:
        """Generates HMAC-MD5 signature for WayForPay"""
        return hmac.new(
            self.secret_key.encode("utf-8"),
            data_str.encode("utf-8"),
            hashlib.md5
        ).hexdigest()

    async def create_invoice(self, amount: float, order_id: str, client_name: str, product_name: str = "Оплата послуг") -> tuple[str, str]:
        """Creates a WayForPay Invoice returning a payment checkout page and invoice ID (orderReference)."""
        import time
        order_date = int(time.time())
        amount_str = str(int(amount)) if float(amount).is_integer() else str(amount)
        
        # Concatenate fields with ';' for signature
        # merchantAccount;merchantDomainName;orderReference;orderDate;amount;currency;productName;productCount;productPrice
        fields = [
            self.merchant_account,
            self.domain,
            order_id,
            str(order_date),
            amount_str,
            "UAH",
            product_name,
            "1",
            amount_str
        ]
        data_string = ";".join(fields)
        signature = self._generate_signature(data_string)

        payload = {
            "transactionType": "CREATE_INVOICE",
            "merchantAccount": self.merchant_account,
            "merchantAuthType": "SimpleSignature",
            "merchantDomainName": self.domain,
            "merchantSignature": signature,
            "apiVersion": 1,
            "orderReference": order_id,
            "orderDate": order_date,
            "amount": int(amount) if float(amount).is_integer() else amount,
            "currency": "UAH",
            "productName": [product_name],
            "productCount": [1],
            "productPrice": [int(amount) if float(amount).is_integer() else amount],
            "serviceUrl": self.webhook_url,
            "redirectUrl": "https://t.me/psy_space_bot"
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(self.base_url, json=payload)
            if response.status_code != 200:
                logger.error("wayforpay_invoice_failed", code=response.status_code, body=response.text)
                raise RuntimeError("Failed to create WayForPay invoice")
                
            res_data = response.json()
            if "invoiceUrl" not in res_data:
                logger.error("wayforpay_invoice_missing_url", response=res_data)
                raise RuntimeError("Failed to retrieve invoice URL from WayForPay")
                
            return res_data["invoiceUrl"], order_id

    def verify_webhook_signature(self, data: dict) -> bool:
        """Verifies that incoming webhook payload signature matches WayForPay cryptographic signature."""
        try:
            order_reference = str(data.get("orderReference", ""))
            status = str(data.get("status", ""))
            time_val = str(data.get("time", ""))
            
            # Signature string: orderReference;status;time
            sign_string = f"{order_reference};{status};{time_val}"
            calculated = self._generate_signature(sign_string)
            return hmac.compare_digest(calculated, data.get("signature", ""))
        except Exception as e:
            logger.error("wayforpay_signature_verification_exception", error=str(e))
            return False

    def generate_webhook_response(self, order_reference: str) -> dict:
        """Generates accepted response payload for WayForPay callback verification."""
        import time
        status = "accept"
        timestamp = int(time.time())
        sign_string = f"{order_reference};{status};{timestamp}"
        signature = self._generate_signature(sign_string)
        
        return {
            "orderReference": order_reference,
            "status": status,
            "time": timestamp,
            "signature": signature
        }

    @staticmethod
    def get_decline_reason(reason_code: int | str | None) -> str:
        """Translates WayForPay decline reason codes to friendly Ukrainian messages."""
        if not reason_code:
            return "невідома помилка платежу"
            
        code = str(reason_code)
        reasons = {
            "1101": "недостатньо коштів на рахунку",
            "1105": "неправильний CVV2/CVC2 код",
            "1107": "термін дії картки закінчився",
            "1108": "обмеження вашого банку на інтернет-платежі або перевищено ліміт",
            "1109": "транзакцію відхилено банком-емітентом",
            "1124": "невірне підтвердження 3D-Secure",
            "1144": "перевищено ліміт кількості операцій",
            "1145": "перевищено ліміт суми для інтернет-оплат",
            "5100": "відхилено платіжною системою (загальна помилка)",
        }
        return reasons.get(code, f"відхилено банком (код: {code})")


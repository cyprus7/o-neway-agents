"""Channel-independent trip commands; HTTP and bots share these transactions."""
from ..checkout import DemoQuoteError, demo_invoice
from ..planner import PriceValidationError, simulate_checkout, validate_option
from .errors import ApplicationError


class TripApplication:
    def __init__(self, store, principal=None):
        self.store = store
        self.principal = principal

    def for_principal(self, principal):
        return TripApplication(self.store, principal)

    def _owner(self):
        user = self.store.get_user(self.principal.user_id) if self.principal else None
        if user is None or user.kind == 'legacy_unclaimed':
            raise ApplicationError(401, 'authentication_required', 'Необходимо войти.')
        return user.user_id

    def session(self, session_id):
        session = self.store.get_owned_session(session_id, self._owner())
        if session is None:
            raise ApplicationError(404, "not_found", "Поездка не найдена.")
        return session

    def create(self, preference_session_id=None, *, apply_memory=False):
        try:
            with self.store.transaction():
                owner = self._owner()
                session = self.store.create_owned_session(owner, preference_session_id)
                if apply_memory:
                    self.store.apply_memory(owner, session)
                    self.store.save_session(session)
                return session
        except KeyError:
            raise ApplicationError(404, 'not_found', 'Поездка не найдена.') from None

    def job(self, job_id):
        job = self.store.get_job(job_id)
        if job is None:
            raise ApplicationError(404, "not_found", "Задание не найдено.")
        self.session(job.session_id)
        return job

    def receipts(self, session_id):
        self.session(session_id)
        return self.store.list_owned_receipts(self._owner(), session_id)

    def list(self, limit=20, cursor=None):
        try:
            return self.store.list_owned_sessions(self._owner(), limit, cursor)
        except ValueError:
            raise ApplicationError(422, 'invalid_pagination', 'Некорректные параметры списка.') from None

    def message(self, session_id, content, *, key, expected_revision, action=None, option_ids=None):
        self.session(session_id)
        return self.store.admit_message(self._owner(), session_id, content, key, expected_revision,
                                        action=action, option_ids=option_ids)

    def refresh(self, session_id, body, *, key):
        self.session(session_id)
        return self.store.admit_refresh(self._owner(), session_id, body, key)

    def cancel(self, job_id):
        self.job(job_id)
        return self.store.cancel_command(self._owner(), job_id)

    def _idle(self, session_id):
        if self.store.active_job(session_id):
            raise ApplicationError(409, "session_busy", "Дождитесь завершения пересчёта поездки.")

    def _option(self, session, option_id):
        try:
            return validate_option(session, option_id)
        except PriceValidationError as exc:
            raise ApplicationError(409, exc.code, str(exc)) from None
        except ValueError as exc:
            self.store.increment("itinerary_validation_errors")
            raise ApplicationError(409, "invalid_option", str(exc)) from None

    def select(self, session_id, option_id, expected_revision=None):
        with self.store.transaction():
            session = self.session(session_id)
            if expected_revision is not None and session.revision != expected_revision:
                raise ApplicationError(409, 'revision_mismatch', 'Версия поездки изменилась.')
            self._idle(session_id)
            self._option(session, option_id)
            session.selected_option_id = option_id
            self.store.save_session(session)
            self.store.increment("options_selected")
            return session

    def _checkout_option(self, session_id, body):
        session = self.session(session_id)
        self._idle(session_id)
        if body.expected_revision is not None and session.revision != body.expected_revision:
            raise ApplicationError(409, "revision_mismatch", "Версия поездки изменилась. Проверьте новый демо-счёт.")
        if session.selected_option_id != body.option_id:
            raise ApplicationError(409, "selection_required", "Сначала выберите вариант из текущего результата поездки.")
        return session, self._option(session, body.option_id)

    def quote(self, session_id, body):
        with self.store.transaction():
            session, option = self._checkout_option(session_id, body)
            return demo_invoice(session, option)

    def checkout(self, session_id, body):
        with self.store.transaction():
            session, option = self._checkout_option(session_id, body)
            try:
                invoice = demo_invoice(session, option, body.quote_id) if body.quote_id else None
            except DemoQuoteError as exc:
                raise ApplicationError(409, exc.code, str(exc)) from None
            previous = self.store.find_checkout(session_id, session.revision, option.id)
            if previous and (invoice is None or previous.get("demo_payment", {}).get("invoice", {}).get("context_id") == invoice["context_id"]):
                return previous
            result = simulate_checkout(session, option, invoice)
            self.store.save_checkout(result)
            self.store.increment("simulated_checkouts")
            self.store.add_event(session_id, "checkout", "completed", result["message"], unchecked_conditions=len(result["warnings"]))
            return result

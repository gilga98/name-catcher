from django.apps import AppConfig


class NamesConfig(AppConfig):
    name = "names"

    def ready(self) -> None:
        from . import checks  # noqa: F401
        self._connect_signals()

    def _connect_signals(self) -> None:
        from allauth.account.signals import user_logged_in, user_signed_up
        from . import accounts, services
        from .models import Board

        def on_user_signed_up(request, user, **kwargs):
            if not accounts.has_current_consent(user):
                accounts.record_consent(user)

        def on_user_logged_in(request, user, **kwargs):
            if not request:
                return
            token = request.session.get("provisional_board")
            if token:
                board = Board.objects.filter(parent_token=token).first()
                if board and not board.members.exists():
                    services.claim_board(board, user, request.session.get("provisional_voter"))
                    request.session.pop("provisional_board", None)
                    request.session.pop("provisional_voter", None)

        user_signed_up.connect(on_user_signed_up, dispatch_uid="names_user_signed_up")
        user_logged_in.connect(on_user_logged_in, dispatch_uid="names_user_logged_in")

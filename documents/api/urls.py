"""URLConf for the documents API, mounted at ``api/v1/documents/``."""

from rest_framework.routers import SimpleRouter

from .views import DocumentViewSet

router = SimpleRouter(trailing_slash=True)
router.register("document", DocumentViewSet, basename="document")

urlpatterns = router.urls

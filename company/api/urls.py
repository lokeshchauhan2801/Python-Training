"""URLConf for the company API, mounted at ``api/v1/company/``."""

from rest_framework.routers import SimpleRouter

from .views import CompanyViewSet

router = SimpleRouter(trailing_slash=True)
router.register("company", CompanyViewSet, basename="company")

urlpatterns = router.urls

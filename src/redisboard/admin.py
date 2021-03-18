from functools import update_wrapper

from django.contrib import admin
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from .models import RedisServer
from .utils import PY3
from .views import inspect, delete
from django.urls import reverse
from django.conf.urls import re_path

if PY3:
    unicode = str


class RedisServerAdmin(admin.ModelAdmin):
    class Media:
        css = {
            'all': ('redisboard/admin.css',)
        }

    list_display = (
        '__unicode__',
        'status',
        'memory',
        'clients',
        'details',
        'cpu_utilization',
        'slowlog',
        'tools',
    )

    list_filter = 'label', 'hostname', 'port'
    ordering = ('hostname', 'port')

    def slowlog(self, obj):
        output = [(float('inf'), 'Total: %d items' % obj.stats['slowlog_len'])]
        for log in obj.stats['slowlog']:
            command = log['command']

            if len(command) > 255:
                command = str(command[:252]) + '...'

            output.append((
                log['duration'],
                u'%.1fms: %r' % (log['duration'] / 1000., command),
            ))
        if output:
            return mark_safe('<br>'.join(l for _, l in sorted(output, reverse=True)))
        else:
            return 'n/a'
    slowlog.allow_tags = True
    slowlog.long_description = _('Slowlog')

    def status(self, obj):
        return obj.stats['status']
    status.long_description = _("Status")

    def memory(self, obj):
        return obj.stats['memory']
    memory.long_description = _("Memory")

    def clients(self, obj):
        return obj.stats['clients']
    clients.long_description = _("Clients")

    def tools(self, obj):
        return mark_safe('<a class="viewlink" href="%s">%s</a>' % (
            reverse("admin:redisboard_redisserver_inspect", args=(obj.id,)),
            unicode(_("Inspect"))
        ))
    tools.allow_tags = True
    tools.long_description = _("Tools")

    def details(self, obj):
        output = []
        brief_details = obj.stats['brief_details']
        for k, v in (brief_details.items() if PY3 else brief_details.iteritems()):
            output.append('<dt>%s</dt><dd>%s</dd>' % (k, v))
        if output:
            return mark_safe('<dl class="details">%s</dl>' % ''.join(output))
        return 'n/a'
    details.allow_tags = True
    details.long_description = _("Details")

    def cpu_utilization(self, obj):
        stats = obj.stats
        if stats['status'] != 'UP':
            return 'n/a'

        data = (
            'used_cpu_sys',
            'used_cpu_sys_children',
            'used_cpu_user',
            'used_cpu_user_children',
        )
        data = dict((k, stats['details'][k]) for k in data)
        total_cpu = sum(data.values() if PY3 else data.itervalues())
        uptime = stats['details']['uptime_in_seconds']
        data['cpu_utilization'] = '%.3f%%' % (total_cpu / uptime if uptime else 0)

        data = sorted(data.items())

        output = []
        for k, v in data:
            k = k.replace('_', ' ')
            output.append('<dt>%s</dt><dd>%s</dd>' % (k, v))

        return mark_safe('<dl class="details">%s</dl>' % ''.join(output))
    cpu_utilization.allow_tags = True
    cpu_utilization.long_description = _('CPU Utilization')

    def get_urls(self):
        urlpatterns = super(RedisServerAdmin, self).get_urls()

        def wrap(view):
            def wrapper(*args, **kwargs):
                return self.admin_site.admin_view(view)(*args, **kwargs)

            return update_wrapper(wrapper, view)

        return [re_path(r'^(\d+)/inspect/$',
                    wrap(self.inspect_view),
                    name='redisboard_redisserver_inspect'),
                re_path(r'^(\d+)/delete/$',
                        wrap(self.delete_key),
                        name='redisboard_redisserver_delete'),
                ] + urlpatterns

    def delete_key(self, request, server_id):
        server = get_object_or_404(RedisServer, id=server_id)
        if request.user.is_superuser:
            return delete(request, server)
        else:
            return HttpResponseForbidden("You can't delete keys.")


    def inspect_view(self, request, server_id):
        server = get_object_or_404(RedisServer, id=server_id)
        if (
            self.has_change_permission(request, server) and
            request.user.has_perm('redisboard.can_inspect')
        ):
            return inspect(request, server)
        else:
            return HttpResponseForbidden("You can't inspect this server.")


admin.site.register(RedisServer, RedisServerAdmin)

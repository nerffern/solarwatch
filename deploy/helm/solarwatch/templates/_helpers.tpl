{{- define "solarwatch.fullname" -}}
{{ .Release.Name }}
{{- end -}}

{{- define "solarwatch.secretName" -}}
{{ .Release.Name }}-secrets
{{- end -}}

{{/*
Build DATABASE_URL from database.* values.
Used by both the web app and the collector — one URL, one Secret.
*/}}
{{- define "solarwatch.databaseUrl" -}}
postgresql+psycopg2://{{ .Values.database.user }}:{{ .Values.database.password }}@{{ .Values.database.host }}:{{ .Values.database.port }}/{{ .Values.database.name }}
{{- end -}}

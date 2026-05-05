{{- define "solarwatch.fullname" -}}
{{ .Release.Name }}
{{- end -}}

{{- define "solarwatch.secretName" -}}
{{ .Release.Name }}-secrets
{{- end -}}

{{/*
Build the DATABASE_URL from database.* values.
Matches the format expected by SQLAlchemy and the .env.example DATABASE_URL option.
*/}}
{{- define "solarwatch.databaseUrl" -}}
postgresql+psycopg2://{{ .Values.database.user }}:{{ .Values.database.password }}@{{ .Values.database.host }}:{{ .Values.database.port }}/{{ .Values.database.name }}
{{- end -}}

{{/*
Same connection string for the collector (uses PG_* vars, not DATABASE_URL).
*/}}
{{- define "solarwatch.pgHost" -}}{{ .Values.database.host }}{{- end -}}
{{- define "solarwatch.pgPort" -}}{{ .Values.database.port }}{{- end -}}
{{- define "solarwatch.pgDb" -}}{{ .Values.database.name }}{{- end -}}
{{- define "solarwatch.pgUser" -}}{{ .Values.database.user }}{{- end -}}
{{- define "solarwatch.pgPass" -}}{{ .Values.database.password }}{{- end -}}

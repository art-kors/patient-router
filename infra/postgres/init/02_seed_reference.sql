-- Справочники: специальности, площадки, врачи
INSERT INTO specialty (code, name, is_surgical) VALUES
    ('gyn_surg',    'Оперирующий гинеколог',     TRUE),
    ('mammologist', 'Маммолог',                  TRUE),
    ('oncologist',  'Онколог',                   TRUE),
    ('gyn_onco',    'Гинеколог-онколог',         TRUE),
    ('surgeon',     'Хирург',                    TRUE),
    ('gastro',      'Гастроэнтеролог',           FALSE),
    ('urologist',   'Уролог',                    TRUE),
    ('nephrologist','Нефролог',                  FALSE),
    ('endocrinologist','Эндокринолог',            FALSE),
    ('vascular',    'Сосудистый хирург',         TRUE),
    ('phlebologist','Флеболог',                  TRUE);

INSERT INTO clinic (code, name, address) VALUES
    ('vdnh',  'ВДНХ',              'Москва, ВДНХ'),
    ('tekst', 'Текстильщики',      'Москва, ул. Текстильщиков'),
    ('senеж', 'Сенежская',         'Москва, Сенежская');

INSERT INTO doctor (external_id, full_name, specialty_id, clinic_id)
SELECT 'doc-01', 'Иванова А. С.', s.id, c.id
FROM specialty s, clinic c
WHERE s.code = 'gyn_surg' AND c.code = 'vdnh';

INSERT INTO doctor (external_id, full_name, specialty_id, clinic_id)
SELECT 'doc-02', 'Петрова М. В.', s.id, c.id
FROM specialty s, clinic c
WHERE s.code = 'mammologist' AND c.code = 'tekst';
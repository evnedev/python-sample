import os
import re
from itertools import chain

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.auth.models import Group
from django.core import signing
from django.db import models
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils.timezone import now
from sorl.thumbnail import get_thumbnail

from countries.list import COUNTRIES_CZ
from employees.cache import cached
from employees.signals import teacher_blocked
from expenses.models import SalaryProfile
from language.models import Language, Morpher
from portal.models import MailMixin
from portal.utils import materials_fs, get_full_url
from portal.utils.dates import days2sec
from user_tests.models import UserTest


class ActiveManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(user__is_active=True)


class Employee(models.Model, MailMixin):
    user = models.OneToOneField(settings.AUTH_USER_MODEL)
    description = models.TextField(blank=True, null=True)
    position = models.CharField(max_length=255, blank=True, null=True)

    contract_name = models.CharField(max_length=255, blank=True)
    passport_number = models.CharField(max_length=255, blank=True)
    address = models.TextField(blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    city = models.CharField(max_length=255, blank=True)

    address_cz = models.TextField(blank=True)
    city_cz = models.CharField(max_length=255, blank=True)

    @property
    def interface_language(self):
        return 'ru'

    objects = models.Manager()
    active = ActiveManager()

    def __str__(self):
        return self.user.first_name

    @property
    def photo(self):
        return get_thumbnail(self.user.image, '250x250', crop='center') if self.user.image else None

    @property
    def profile_photo(self):
        return EmployeeProfile.get(self.user).photo

    @property
    def name(self):
        return self.user.first_name

    @property
    def full_name(self):
        return self.user.get_full_name()

    @property
    def last_name(self):
        return self.user.last_name

    @property
    def email(self):
        return self.user.email

    @property
    def country(self):
        return self.user.country

    @property
    def country_name(self):
        return self.user.get_country_display()

    @property
    def country_name_cz(self):
        return COUNTRIES_CZ.get(self.user.country, None) or self.country_name

    @property
    def full_label(self):
        label = self.user.helpdeskprofile.label if hasattr(self.user, 'helpdeskprofile') else None
        return u'%s (%s)' % (self.user.first_name, label) if label else self.user.first_name

    def _get_user(self) -> settings.AUTH_USER_MODEL:
        return self.user

    class Meta:
        ordering = ['user__first_name']
        abstract = True


# class CurrentManager(Employee):
#      class Meta:
#         db_table = 'employees_manager'



class TeacherManager(ActiveManager):
    def for_language(self, language_code):
        return self.get_queryset().filter(
            Q(language__code=language_code) | Q(additional_languages__code=language_code)
        )


class Teacher(Employee, Morpher):

    DEMOS_MIN = 10
    METHODS = (
        ('unistream', 'Unistream'),
        ('paypal', 'PayPal'),
        ('account', 'Bank account'),
        ('cash', 'Cash')
    )
    language = models.ForeignKey(Language, related_name='teachers')
    additional_languages = models.ManyToManyField(Language)
    russian = models.BooleanField(default=False)
    native = models.BooleanField(default=False)
    language_support = models.BooleanField(default=False)

    skype = models.CharField(max_length=255, blank=True, null=True)
    skype_password = models.CharField(max_length=255, blank=True, null=True)

    work_since = models.DateField(blank=True, null=True)
    contract_end = models.DateField(blank=True, null=True)

    active = TeacherManager()

    def has_unfinished_lessons(self):
        from courses.models import SingleTeacherLesson
        return SingleTeacherLesson.objects.filter(teacher_id=self.pk, finished=False).exists()

    def generate_position(self):
        languages = ', '.join([l.in_case('gent') for l in Language.objects.filter(code__in=self.all_languages_codes)])
        return 'Преподаватель {} язык{}{}'.format(
            languages,
            'ов' if ', ' in languages else 'а',
            ', носитель' if self.native else ''
        )

    @property
    def phone(self):
        return self.user.phone

    @property
    def salary_profile(self):
        try:
            return self.user.salaryprofile.pk
        except SalaryProfile.DoesNotExist:
            return None

    @property
    def employee_profile(self):
        try:
            return self.user.employeeprofile.pk
        except EmployeeProfile.DoesNotExist:
            return None

    @property
    def manager_url(self):
        return get_full_url('manager:manager_teacher_update', args=(self.pk,))

    @property
    def is_active(self):
        return self.user.is_active

    @property
    def interface_language(self):
        return 'ru' if self.russian else 'en'

    @property
    def all_languages_codes(self):
        return {self.language.code} | set(self.additional_languages.all().values_list('code', flat=True))

    @property
    def languages_contract(self):
        return ', '.join([
            l.en_name for l in Language.objects.filter(code__in=self.all_languages_codes)
        ])

    @property
    def languages_contract_cz(self):
        return ', '.join([l.cz_gent for l in Language.objects.filter(code__in=self.all_languages_codes)])

    def get_basic_materials(self):
        if not self.russian:
            return []
        codes = chain.from_iterable([(
            '{}-BASIC-S'.format(c.upper()),
            '{}-BASIC-N'.format(c.upper())
        ) for c in self.all_languages_codes])
        result = []
        for code in codes:
            path = os.path.join(settings.BASE_DIR, 'materials/courses/templates/ru', code)
            if not os.path.exists(path):
                continue
            result += [{
                'code': code,
                'name': f,
                'url': '{}?sign={}'.format(
                    get_full_url('teacher_material_template', args=('ru', code, f)),
                    signing.dumps((self.user.pk, f, code))
                )
            } for f in os.listdir(path) if re.match(r'module\d+(_theory|)\.pdf', f)]
        return result

    def language_for_student(self, student):
        from courses.models import SingleTeacherLesson
        languages = set(SingleTeacherLesson.objects.filter(course__student=student, teacher=self).values_list(
            'course__base_course__language__machine_name', flat=True
        ))
        teacher_languages = set(
            list(self.additional_languages.values_list('machine_name', flat=True)) + [self.language.machine_name]
        )
        intersection = languages.intersection(teacher_languages)
        try:
            return intersection.pop()
        except KeyError:
            return self.language.machine_name

    def _get_right_form(self, word):
        for p in word:
            if 'nomn' in p.tag:
                return p
        return word[0]

    @property
    def _case_attr(self):
        return self.name

    def _case_transform(self, word):
        return word.capitalize()

    @staticmethod
    def _assign_tests(teacher):
        due = now().date() + relativedelta(months=1)

        UserTest.objects.create_test(user=teacher.user, asset='webinar1', due_date=due)
        UserTest.objects.create_test(user=teacher.user, asset='webinar2', due_date=due)

    @staticmethod
    def create(user: settings.AUTH_USER_MODEL, password: str, **kwargs):
        send_greeting_email = kwargs.pop('__send_greeting_email', True)
        additional_languages = kwargs.pop('additional_languages', None) or []
        teacher = Teacher.objects.create(user=user, **kwargs)
        teacher.position = teacher.generate_position()
        teacher.save()
        for lang_pk in additional_languages:
            teacher.additional_languages.add(lang_pk)

        SalaryProfile.objects.create(user=user)
        teacher.user.groups.add(Group.objects.get_or_create(name='Helpdesk support')[0])

        if teacher.russian:
            Teacher._assign_tests(teacher)

        if send_greeting_email:
            teacher.send_html_mail('Access to teacher\'s dashboard',
                                   render_to_string('manager/create_teacher_email.html', {
                                       'name': teacher.name,
                                       'password': password,
                                       'teacher': teacher,
                                       'native': teacher.native,
                                   }))

        return teacher

    @property
    def currency(self):
        try:
            return self.user.salaryprofile.currency
        except SalaryProfile.DoesNotExist:
            return 'EUR'

    @property
    def preferable_pm(self):
        try:
            return self.user.salaryprofile.preferable_pm
        except SalaryProfile.DoesNotExist:
            return None

    @property
    def rate(self):
        try:
            return self.user.salaryprofile.rate
        except SalaryProfile.DoesNotExist:
            return 0

    @property
    def salary(self):
        try:
            return self.user.salaryprofile.salary
        except SalaryProfile.DoesNotExist:
            return 0

    @property
    def work_duration_upper_bound(self):
        duration = self.user.salaryprofile.work_duration_upper_bound if self.salary_profile else None
        return duration

    @property
    @cached(lambda t: 'FINISHED_DEMO__{}'.format(t.pk), timeout=days2sec(1))
    def finished_demo_lessons(self):
        return self._finished_demo_lessons().count()

    def _finished_demo_lessons(self):
        return self.singleteacherlesson_set.filter(
            Q(course__base_course__code='DEMO') | Q(course__base_course__code='NATIVE-DEMO'),
            finished=True)

    @property
    @cached(lambda t: 'PAID_AFTER_DEMO__{}'.format(t.pk), timeout=days2sec(1))
    def paid_after_demo(self):
        from courses.models.with_teacher import StudentCourse
        demo_lessons = self._finished_demo_lessons()
        students_pay = 0
        for lesson in demo_lessons:
            student = lesson.course.student
            if StudentCourse.objects.filter(student=student) \
                    .exclude(created__lt=lesson.start) \
                    .exclude(base_course__code='DEMO') \
                    .exclude(base_course__code='NATIVE-DEMO') \
                    .exists():
                students_pay += 1
        return students_pay

    @property
    def conversion(self):
        students_demo = self.finished_demo_lessons
        if students_demo < Teacher.DEMOS_MIN:
            return 1.0

        return round(self.paid_after_demo / students_demo, 2)

    def block(self):
        self.freetimetemplate_set.all().update(available=False)
        self.freetime_set.all().update(available=False)
        user = self.user
        user.is_active = False
        user.set_unusable_password()
        user.save()
        teacher_blocked.send_robust(Teacher, teacher=self)


class Manager(Employee):

    class Meta:
        db_table = 'employees_manager'



class EmployeeProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL)
    additional_info = models.TextField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    position = models.CharField(max_length=255, blank=True, null=True)

    objects = models.Manager()
    active = ActiveManager()

    @staticmethod
    def get(user):
        return getattr(user, 'employeeprofile', None)

    @property
    def photo(self):
        return self.user.image

    @property
    def name(self):
        return self.user.first_name

    @property
    def last_name(self):
        return self.user.last_name

    @property
    def full_name(self):
        return self.user.get_full_name()

    def __str__(self):
        return self.full_name


class TeacherMaterial(models.Model):
    language = models.ForeignKey(Language, related_name='teacher_materials')
    attachment = models.FileField(storage=materials_fs, upload_to='teachers')
    russian = models.BooleanField(default=False)
    native = models.BooleanField(default=False)

    @property
    def name(self):
        try:
            return os.path.basename(self.attachment.file.name)
        except FileNotFoundError:
            return '-- file not found --'

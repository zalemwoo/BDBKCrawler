# -*- coding: utf-8 -*-

import os
import errno 
import re
import json
import traceback

import scrapy
import redis # https://github.com/andymccurdy/redis-py

from bdbk.items import CategoryItem
from bdbk.items import PersonItem
from bdbk.items import ImageItem

from bdbk.utils import mkdir

# generic settings
BAIDU_DOMAIN = ['baidu.com']

class CategorySpider(scrapy.Spider):
    name = 'bdbk.category'
    allowed_domains = BAIDU_DOMAIN

    categories = dict()

    def __init__(self, url=None, *args, **kwargs):
        self.start_page = url
        self.from_category = False
        if self.start_page == None:
            self.from_category = True
        super(CategorySpider, self).__init__(*args, **kwargs)

    def start_requests(self):
        # create dir
        self.data_path = os.path.join('.', self.settings["DATA_PATH"])

        try:
            mkdir(self.data_path)
        except OSError, err:
            raise
        
        # redis client
        try:
            self.redis_client = redis.Redis(host=self.settings["REDIS_SERVER_HOST"], port=self.settings["REDIS_SERVER_PORT"], db=0)
            self.redis_client.flushdb()

        except redis.RedisError, err:
            raise err

        if self.start_page == None:
            self.start_page = self.settings['START_PAGE']
            request = scrapy.Request(self.start_page, self.parse)
        else:
            request = scrapy.Request(self.start_page, self.parse_person)

        self.logger.info("Start crawling url: %s" %self.start_page)

        yield request

    def parse(self, response):
        for sel in response.xpath('//a[contains(@href, "taglist")]'):
            url = response.urljoin(sel.xpath('@href').extract()[0])
            for i in range(0, 750 + 1, 10):
                list_url = url + '&offset={0}'.format(i)
                request = scrapy.Request(list_url, callback = self.parse_category_list)
                yield request

    def parse_category_list(self, response):
        for sel in response.xpath('//a[contains(@href, "/view/")]'):
            url = response.urljoin(sel.xpath('@href').extract()[0].split('?')[0])
            request = scrapy.Request(url, callback = self.parse_person)
            yield request


    def parse_person(self, response):
        url = response.url.split('?')[0]

        '''
        check if scanned:
        'http://baike.baidu.com/subview/3996/3996.htm'
        will get '3996' as an unique id(uid)
        if got nothing, use the url as uid.
        ''' 
        r = re.compile(r'\D*(\d+)\D*') 
        uid = r.findall(url)
        if len(uid) > 0:
          uid = uid[-1]
        else:
          uid = url

        scann_cnt = self.redis_client.get(uid)
        if scann_cnt != None:
            scann_cnt = int(scann_cnt) + 1
            self.redis_client.set(uid, scann_cnt)
            return

        self.redis_client.set(uid, 1)

        # the 'keywords' meta must contains '人物'
        kwlist = response.xpath('//meta[@name="keywords"]/@content').extract()
        if len(kwlist) == 0:
          return

        keywords = kwlist[0].encode('utf-8', 'ignore')
        if keywords.find('人物') == -1:
            return

        description = response.xpath('//meta[@name="description"]/@content').extract()[0].encode('utf-8', 'ignore')

        person_item = PersonItem()
        person_item['name'] = response.xpath('//h1/text()').extract()[0].encode('utf-8', 'ignore')
        person_item['url'] = url
        person_item['keywords'] = keywords
        person_item['description'] = description

        # get person tags (人物标签)
        person_tags = list()
        for sel in response.xpath('//span[@class="taglist"]'):
          tag = sel.xpath('text()').extract()[0].replace('\n', '').encode('utf-8', 'ignore')
          person_tags.append(tag)
          if self.categories.has_key(tag):
              self.categories[tag] = self.categories[tag] + 1
          else:
              self.categories[tag] = 1
        person_item['tags'] = ' '.join(person_tags)

        summary_pic = response.xpath('//div[@class="summary-pic"]/a/img/@src').extract()
        if len(summary_pic) > 0:
            summary_pic = summary_pic[0].split('/')[-1].split('.')[0]
        else:
            summary_pic = ''
        person_item['summary_pic'] = summary_pic

        # for the data pipeline
        yield person_item
        yield self.categories

        # crawling image gallery (图册)
        # for url in response.xpath('//div[@class="summary-pic"]/a/@href').extract()
        for url in response.xpath('//a[contains(@href, "/picture/")]/@href').extract():
            image_gallery_url = response.urljoin(url.split('?')[0])
            request = scrapy.Request(image_gallery_url, callback = self.parse_image_gallery)
            request.meta["person_info"] = person_item
            yield request

        # follow link that which url contains |view|(view/subview)
        for sel in response.xpath('//a[contains(@href, "view")]'):
            url = response.urljoin(sel.xpath('@href').extract()[0].split('?')[0])
            request = scrapy.Request(url, callback = self.parse_person)
            yield request

    def parse_image_gallery(self, response):
        person_info = response.meta['person_info']
        self.logger.info('Found Photo Gallery from : %s', response.url)
        album_info_str = "{%s}" % response.xpath('//body/script/text()').re(r'albums:.*lemmaId:')[0].replace('albums', '"albums"').replace(',lemmaId:','')
        album_info_dic = None
        try:
            album_info_dic = json.loads(album_info_str)
            album_info_dic = album_info_dic['albums']
        except Exception, e:
            self.logger.error('json parse album info error. url: %s, err: %r', response.url, e)
            return
        if isinstance(album_info_dic, list):
            album_info_dic = album_info_dic[0]

        pictures = []
        cover_pics = []
        try:
            pictures.append(album_info_dic['pictures'])
            cover_pics.append(album_info_dic['coverpic'])
        except KeyError, e:
            try:
                for k,v in album_info_dic.items():
                  if v.has_key('pictures'):
                    pictures.append(v['pictures'])
                    cover_pics.append(v['coverpic'])
            except Exception, e:
                self.logger.error('parse pictures info error. url: %s, err: %r', 
                      response.url, e)
                return
        except Exception, e:
            self.logger.error('parse pictures info error. url: %s, err: %r', 
                response.url, e)
            return

        for p in pictures:
            for picture_info in p:
              image_item = ImageItem()
              try:
                prefer_index = str(picture_info['type']['oriWithWater'])
                image = picture_info['sizes'][prefer_index]
                src = picture_info['src']
                # src
                image_item['src'] = src
                # is_cover
                if src in cover_pics:
                    image_item['is_cover'] = True
                else:
                    image_item['is_cover'] = False
                # desc
                description = picture_info['desc'].encode('utf8', 'ignore')
                image_item['desc'] = description
                image_item['url'] = image['url']
                image_item['width'] = image['width']
                image_item['height'] = image['height']
                image_item['size'] = image['size']
                image_item['person_name'] = person_info['name']
                image_item['person_url'] = person_info['url']
              except Exception, e:
                self.logger.error('parse pictures info error. picture: %r, err: %r \n TRACE: %s', 
                  picture_info, e, traceback.format_exc())
                continue

              src = image_item['src']
              scann_cnt = self.redis_client.get(src)
              if scann_cnt != None:
                scann_cnt = int(scann_cnt) + 1
                self.redis_client.set(src, scann_cnt)
                continue 

              self.redis_client.set(src, 1)

              request = scrapy.Request(image_item['url'], callback = self.download_image)
              request.meta["image_info"] = image_item
              yield request

    def download_image(self, response):
        image_info = response.meta['image_info']
        file_name = response.url.split('/')[-1]
        path_part = os.path.join(file_name[0:2], file_name[2:4])
        image_dir = os.path.join('.', self.data_path, 'images', path_part)
        file_path = os.path.join(image_dir, file_name)

        # check file if exist
        if os.path.isfile(file_path):
            self.logger.warning("download_image() file exist. image_info: %r" , image_info)
            return

        # mime 
        image_info['mime'] = response.headers['Content-Type']
        image_info['file_name'] = file_name
        image_info['file_path'] = os.path.join(path_part, file_name)

        try:
            mkdir(image_dir)
        except OSError, err:
            raise

        try:
          with open(file_path, 'wb') as f:
            f.write(response.body)
        except Exception, err:
          this.logger.error("image file write error. file: %s, err: %r", file_name, err)
          raise

        self.logger.info('Image saved to: %s', file_path)

        yield image_info

class BDBKSpider(CategorySpider):
    name = 'bdbk'